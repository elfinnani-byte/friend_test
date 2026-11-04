"""크레딧(원장 방식). 잔액은 기록(delta)의 합계이고, 기록은 더하기만 한다.

- 처음 온 사용자: +FREE_SIGNUP_CREDITS (ref signup:<사용자ID>)
- 메시지를 받자마자: -CREDIT_COST_PER_MESSAGE (ref msg:<메시지ID>)
- 답을 만들다 실패하면: +CREDIT_COST_PER_MESSAGE (ref refund:<메시지ID>)
ref 는 원장에서 유일하므로 같은 지급, 차감, 되돌림이 두 번 들어가지 않는다.
숫자는 app/config.py 에만 있다. 화면은 잔액을 계산하지 않고 GET /api/credits 로 받아 쓴다.
"""
from app.config import CREDIT_COST_PER_MESSAGE, FREE_SIGNUP_CREDITS
from app.logs import log
from app.store.base import DuplicateReference

INSUFFICIENT = {"error": "insufficient_credits", "message": "크레딧을 다 썼어요. 충전하면 대화를 이어갈 수 있어요."}


def grant_signup(store, uid):
    """원장에 기록이 하나도 없는 사용자에게 무료 크레딧을 한 번 준다."""
    if store.recent_credits(uid, 1):
        return
    try:
        store.add_credit(uid, FREE_SIGNUP_CREDITS, "signup", f"signup:{uid}")
        log("INFO", "무료 크레딧 지급", amount=FREE_SIGNUP_CREDITS)
    except DuplicateReference:
        pass  # 동시에 온 다른 요청이 먼저 줬다


def balance(store, uid):
    grant_signup(store, uid)
    return store.credit_balance(uid)


def charge(store, uid, message_id):
    """메시지 하나만큼 차감한다. 잔액이 모자라면 차감하지 않고 (False, 잔액)을 돌려준다.

    잔액을 본 뒤 차감하는 사이에 다른 요청이 끼어들 수 있어, 차감한 뒤 잔액이 음수면 바로 되돌리고 거절한다.
    """
    current = balance(store, uid)
    if current < CREDIT_COST_PER_MESSAGE:
        return False, current
    store.add_credit(uid, -CREDIT_COST_PER_MESSAGE, "message", f"msg:{message_id}")
    after = store.credit_balance(uid)
    if after < 0:
        refund(store, uid, message_id)
        return False, store.credit_balance(uid)
    return True, after


def refund(store, uid, message_id):
    """차감을 되돌린다. 이미 되돌렸으면 아무것도 하지 않는다."""
    try:
        store.add_credit(uid, CREDIT_COST_PER_MESSAGE, "refund", f"refund:{message_id}")
        log("INFO", "크레딧 되돌림", amount=CREDIT_COST_PER_MESSAGE)
    except DuplicateReference:
        pass
