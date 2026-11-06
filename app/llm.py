"""LLM 호출을 한곳에 모아 둔다.

OpenAI와 Gemini는 같은 OpenAI SDK로 부르고, 주소(base_url)와 모델 이름만 다르다.
Claude는 Anthropic SDK로 따로 부른다. 앱 안에서 쓰는 메시지 형식은 OpenAI 형식이라서, ClaudeLLM이 Claude 형식으로 바꿔 보낸다.
fake 는 키 없이 화면과 흐름을 확인하거나 테스트할 때 쓰는 가짜 응답이다.

세 클래스 모두 호출이 끝나면 tracing.record 로 같은 내용을 남긴다: 실제로 답한 모델, 보낸 메시지, 답, 입력·출력 토큰 수.
complete_json, complete_text 는 지금 열린 generation 에, chat_stream 은 넘겨받은 observation 에 적는다.
complete_vision_json(퀴즈 풀기)은 이미지 한 장과 함께 한 번만 부르고, 트레이스에는 이미지 대신 trace_input 을 남긴다.
"""
import base64
import json
import re
import time

from app import tracing


class OpenAICompatibleLLM:
    can_reason = True  # 도구 선택, 기억 정리를 모델에게 맡길 수 있다

    def __init__(self, api_key, model, base_url=None):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)
        self.model = model
        # 스트리밍 토큰 수(include_usage)는 OpenAI 주소에서만 켠다. 호환 주소(Gemini)에서 되는지는 확인하지 않았다.
        self.stream_usage = base_url is None

    def record(self, observation, model, messages, text, usage):
        tracing.record(observation, model=model or self.model, input=messages, output=text,
                       input_tokens=getattr(usage, "prompt_tokens", None),
                       output_tokens=getattr(usage, "completion_tokens", None))

    def complete_json(self, system, user):
        """도구 선택처럼 정해진 형식의 답이 필요할 때. JSON 하나를 받아 dict로 돌려준다."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, response_format={"type": "json_object"})
        text = response.choices[0].message.content
        self.record(None, response.model, messages, text, response.usage)
        return json.loads(text)

    def complete_text(self, messages):
        response = self.client.chat.completions.create(model=self.model, messages=messages)
        text = response.choices[0].message.content.strip()
        self.record(None, response.model, messages, text, response.usage)
        return text

    def complete_vision_json(self, system, text, image, max_tokens, trace_input):
        """이미지 한 장(image: {"url"} 또는 {"data", "media_type"})과 질문으로 JSON 하나를 받는다."""
        content = [{"type": "text", "text": text}]
        if image is not None:
            url = image["url"] if "url" in image else \
                f"data:{image['media_type']};base64,{base64.b64encode(image['data']).decode()}"
            content.append({"type": "image_url", "image_url": {"url": url}})
        # OpenAI 주소는 max_completion_tokens, 호환 주소(Gemini)는 max_tokens 로 출력 상한을 준다
        limit = {"max_completion_tokens": max_tokens} if self.stream_usage else {"max_tokens": max_tokens}
        response = self.client.chat.completions.create(
            model=self.model, response_format={"type": "json_object"}, **limit,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}])
        answer = response.choices[0].message.content
        self.record(None, response.model, trace_input, answer, response.usage)
        return json.loads(answer)

    def chat_stream(self, messages, observation=None):
        """답을 만들어지는 대로 조금씩 돌려준다(스트리밍)."""
        extra = {"stream_options": {"include_usage": True}} if self.stream_usage else {}
        response = self.client.chat.completions.create(model=self.model, messages=messages, stream=True, **extra)
        model, usage, reply = None, None, ""
        for event in response:
            model = getattr(event, "model", None) or model
            usage = getattr(event, "usage", None) or usage
            if event.choices and event.choices[0].delta.content:
                if not reply:
                    tracing.mark_first_token(observation)
                reply += event.choices[0].delta.content
                yield event.choices[0].delta.content
        if observation is not None:
            self.record(observation, model, messages, reply, usage)


class ClaudeLLM:
    can_reason = True

    # 안전 분류기가 요청을 거절하면, 같은 요청을 다른 Claude 모델이 이어서 답하게 한다.
    BETAS = ["server-side-fallback-2026-07-01"]
    # 대화 앱이라 생각 깊이(effort)를 낮게 둔다. 답이 빨라지고 비용이 줄어든다.
    OUTPUT_CONFIG = {"effort": "low"}
    MAX_TOKENS = 16000
    REFUSAL_NOTICE = "이 요청에는 답을 만들지 못했어요. 다른 말로 다시 보내 주세요."

    def __init__(self, api_key, model):
        import anthropic

        # SDK 자체 OpenTelemetry 계측을 끈다. 켜 두면 Langfuse가 등록한 전역 provider로 anthropic.messages.create span이
        # 나가서, tracing.record 로 남기는 generation 과 같은 토큰·비용이 한 번 더 잡힌다.
        # https://platform.claude.com/docs/en/manage-claude/opentelemetry/sdk
        self.client = anthropic.Anthropic(api_key=api_key, timeout=60, open_telemetry=False)
        self.model = model

    @staticmethod
    def to_claude(messages):
        """OpenAI 형식 메시지를 (system, messages)로 나눈다.

        Claude는 시스템 지시문을 messages 밖의 system 으로 받고, 대화가 user 차례로 시작해야 한다.
        같은 차례가 이어지면 하나로 합친다.
        """
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = []
        for m in messages:
            if m["role"] == "system":
                continue
            if turns and turns[-1]["role"] == m["role"]:
                turns[-1]["content"] += "\n\n" + m["content"]
            else:
                turns.append({"role": m["role"], "content": m["content"]})
        if not turns or turns[0]["role"] != "user":
            turns.insert(0, {"role": "user", "content": "(대화를 이어 갑니다)"})
        return system, turns

    def _request(self, system, turns):
        return dict(model=self.model, max_tokens=self.MAX_TOKENS, system=system, messages=turns,
                    output_config=self.OUTPUT_CONFIG, betas=self.BETAS, fallbacks="default")

    def _text(self, response):
        if response.stop_reason == "refusal":
            return self.REFUSAL_NOTICE
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def record(self, observation, response, messages, text):
        usage = getattr(response, "usage", None)
        tracing.record(observation, model=getattr(response, "model", None) or self.model, input=messages, output=text,
                       input_tokens=getattr(usage, "input_tokens", None),
                       output_tokens=getattr(usage, "output_tokens", None))

    def complete_json(self, system, user):
        """정해진 형식의 답이 필요할 때. 답에서 JSON 객체 하나를 찾아 dict로 돌려준다."""
        system = system + "\n\n반드시 JSON 객체 하나만 출력한다. 설명이나 코드 블록 표시는 붙이지 않는다."
        response = self.client.beta.messages.create(**self._request(system, [{"role": "user", "content": user}]))
        text = self._text(response)
        self.record(None, response, [{"role": "system", "content": system}, {"role": "user", "content": user}], text)
        if response.stop_reason == "refusal":
            raise ValueError("Claude가 요청을 거절했습니다.")
        return parse_json_object(text)

    def complete_text(self, messages):
        system, turns = self.to_claude(messages)
        response = self.client.beta.messages.create(**self._request(system, turns))
        text = self._text(response)
        self.record(None, response, messages, text)
        return text

    # 서버 쪽 대체 모델(fallbacks)을 받는 모델. Haiku 등 다른 모델에는 보내지 않는다
    FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

    def complete_vision_json(self, system, text, image, max_tokens, trace_input):
        """이미지 한 장과 질문으로 JSON 하나를 받는다. 빠른 응답을 위해 effort low, 출력 상한 max_tokens."""
        if image is None:
            content = [{"type": "text", "text": text}]
        else:
            source = {"type": "url", "url": image["url"]} if "url" in image else \
                {"type": "base64", "media_type": image["media_type"], "data": base64.standard_b64encode(image["data"]).decode()}
            content = [{"type": "image", "source": source}, {"type": "text", "text": text}]
        system = system + "\n\n반드시 JSON 객체 하나만 출력한다. 설명이나 코드 블록 표시는 붙이지 않는다."
        request = dict(model=self.model, max_tokens=max_tokens, system=system, output_config=self.OUTPUT_CONFIG,
                       messages=[{"role": "user", "content": content}])
        if self.model in self.FALLBACK_MODELS:
            response = self.client.beta.messages.create(**request, betas=self.BETAS, fallbacks="default")
        else:
            response = self.client.messages.create(**request)
        answer = self._text(response)
        self.record(None, response, trace_input, answer)
        if response.stop_reason == "refusal":
            raise ValueError("Claude가 요청을 거절했습니다.")
        return parse_json_object(answer)

    def chat_stream(self, messages, observation=None):
        """답을 만들어지는 대로 조금씩 돌려준다(스트리밍)."""
        system, turns = self.to_claude(messages)
        with self.client.beta.messages.stream(**self._request(system, turns)) as stream:
            reply = ""
            for text in stream.text_stream:
                if not reply:
                    tracing.mark_first_token(observation)
                reply += text
                yield text
            final = stream.get_final_message()
            if final.stop_reason == "refusal" and not reply:
                reply = self.REFUSAL_NOTICE
                yield reply
            if observation is not None:
                self.record(observation, final, messages, reply)


def parse_json_object(text):
    """모델 답에서 첫 번째 JSON 객체를 꺼낸다. 없으면 ValueError."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("JSON 응답을 찾지 못했습니다.")
    return json.loads(match.group(0))


class FakeLLM:
    """AI를 부르지 않는다. 도구 선택과 기억 정리는 키워드 규칙이 맡고(can_reason=False),
    답은 지시문에 들어 있는 도구 결과와 캐릭터 이름으로 짧게 만든다.

    비용이 들지 않고 같은 입력에는 늘 같은 답을 돌려주므로, 키 없이 화면을 확인하거나 테스트할 때 쓴다.
    """

    can_reason = False
    model = "fake"

    def __init__(self, delay=0.02):
        self.delay = delay

    def _reply(self, messages):
        system = messages[0]["content"]
        polite = "존댓말" in system
        name = re.search(r"이름: (.+)", system)
        name = name.group(1).strip() if name else "친구"
        tool_lines = []
        if "[도구 결과]" in system:
            block = system.split("[도구 결과]", 1)[1].split("\n\n", 1)[0]
            tool_lines = [line[2:] for line in block.splitlines() if line.startswith("- ")]
        user = messages[-1]["content"] if messages[-1]["role"] == "user" else ""

        if "[되묻기]" in system:
            body = "무슨 말인지 조금 더 알려 줄래요?" if polite else "무슨 말인지 조금만 더 알려 줄래?"
        elif tool_lines:
            body = " ".join(tool_lines)
        elif user.startswith("(앱을 열었다"):  # 먼저 말 걸기
            body = f"안녕하세요, {name}예요. 오늘은 어떻게 지내요?" if polite else f"안녕, 나 {name}! 오늘은 어떻게 지냈어?"
        else:
            body = "그랬군요. 조금 더 이야기해 줄래요?" if polite else "그랬구나. 조금 더 얘기해 줄래?"
        return f"(가짜 응답) {body}"

    def complete_text(self, messages):
        text = self._reply(messages)
        tracing.record(None, model=self.model, input=messages, output=text)
        return text

    def complete_vision_json(self, system, text, image, max_tokens, trace_input):
        """퀴즈 풀기 가짜 답. 객관식은 화면 번호 1, 단답은 '가짜 답'."""
        answer = {"answer": 1 if "[객관식]" in text else "가짜 답", "reason": "(가짜 응답) 정해 둔 답입니다."}
        tracing.record(None, model=self.model, input=trace_input, output=json.dumps(answer, ensure_ascii=False))
        return answer

    def chat_stream(self, messages, observation=None):
        text = self._reply(messages)
        for i in range(0, len(text), 4):
            time.sleep(self.delay)
            if i == 0:
                tracing.mark_first_token(observation)
            yield text[i:i + 4]
        if observation is not None:
            tracing.record(observation, model=self.model, input=messages, output=text)


def make_llm(settings, model=None):
    """model 을 주면 그 모델로 만든다(퀴즈 풀기의 QUIZ_MODEL). 키와 제공자는 같다."""
    model = model or settings.model
    if settings.provider == "fake":
        return FakeLLM()
    if settings.provider == "claude":
        return ClaudeLLM(settings.api_key, model)
    return OpenAICompatibleLLM(settings.api_key, model, settings.base_url)
