"""功能：桌面端前端驱动——通过 CDP 在真实界面上发消息并读取回复。

只走用户会走的那条路：点输入框、填文本、点发送、等消息气泡出现。不直连后端
WebSocket，这样验证的才是「用户看到的行为」。

用法：
    py desktop_driver.py --check                    # 检查界面元素是否就位
    py desktop_driver.py --send "你好"              # 发一条并等回复
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

CDP_PORT = 9333
SEND_TIMEOUT_SEC = 600
POLL_INTERVAL_SEC = 0.6
MAX_CARD_ANSWERS = 40


def _page_ws_url() -> str | None:
    """功能：取当前页面标签的 WebSocket 调试地址。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=6
        ) as response:
            tabs = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return None
    for tab in tabs:
        if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
            return str(tab["webSocketDebuggerUrl"])
    return None


def evaluate(expression: str, timeout: float = 30.0, *, retries: int = 3) -> object:
    """功能：在页面里执行一段表达式并取回结果（每次新建连接，抗导航失效）。

    CDP 在页面重绘/导航时会短暂断开，快速重试能跨过这个窗口；
    不重试就会把一次偶发断开当成用例失败。
    """
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            return _evaluate_once(expression, timeout)
        except Exception as exc:  # noqa: BLE001 - 重试后仍失败才上报
            last_error = exc
            # 功能：CDP 不可用时快速失败。
            # 主进程被占满时单次返回就要 30s，
            # 重试间隔再拉长会让一次 evaluate 耗掉数分钟。
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError(f"CDP 评估失败: {type(last_error).__name__}: {last_error}")


def _evaluate_once(expression: str, timeout: float) -> object:
    """功能：单次 CDP 评估（不重试）。"""
    import websocket

    url = _page_ws_url()
    if url is None:
        raise RuntimeError("没有可调试的页面")
    ws = websocket.create_connection(url, timeout=timeout + 8, suppress_origin=True)
    try:
        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": False,
                        "timeout": int(timeout * 1000),
                    },
                }
            )
        )
        deadline = time.time() + timeout + 6
        while time.time() < deadline:
            message = json.loads(ws.recv())
            if message.get("id") != 1:
                continue
            result = message.get("result") or {}
            if result.get("exceptionDetails"):
                raise RuntimeError(str(result["exceptionDetails"])[:200])
            return (result.get("result") or {}).get("value")
        raise TimeoutError("CDP 响应超时")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def check_ui() -> dict[str, object]:
    """功能：确认输入框/发送键/消息列表都在位，返回界面快照。"""
    script = """(() => {
      const composer = document.querySelector('[data-testid="composer"]');
      const list = document.querySelector('[data-testid="message-list"]');
      return JSON.stringify({
        url: location.href.split('/').slice(-1)[0],
        composer: Boolean(composer),
        messageList: Boolean(list),
        banners: document.querySelectorAll('[data-testid="message-bubble"]').length,
        bodyLen: (document.body.innerText || '').length
      });
    })()"""
    raw = evaluate(script)
    return json.loads(raw) if isinstance(raw, str) else {}


def wait_until_idle(*, timeout: float = 900.0) -> bool:
    """功能：等待上一轮生成结束（停止按钮消失、发送按钮回来）。

    生成中时输入区会把发送键换成停止键，此时点发送没有意义；
    不等就发会把「正在生成」误判成「发送失败」。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 功能：等待期间也要应答交互卡。
        # 多问卡/审批卡不点，生成键永远不会消失，这里就会一直等到超时，
        # 外部只能看到「上一轮未结束」而不知道是没人点卡。
        try:
            from card_responder import answer_once, detect_pending

            cards = detect_pending()
            if (
                cards.get("card")
                or cards.get("permissionOnce")
                or cards.get("submit")
                or cards.get("execute")
            ):
                for _ in range(6):
                    if not answer_once():
                        break
                    time.sleep(0.25)
                continue
        except Exception:
            pass
        try:
            raw = evaluate(
                "(() => JSON.stringify({"
                " stop: Boolean(document.querySelector('[data-testid=\"composer-stop\"]')),"
                " send: Boolean(document.querySelector('[data-testid=\"composer-send\"]'))"
                "}))()",
                timeout=25,
            )
        except Exception:
            time.sleep(1.0)
            continue
        state = json.loads(raw) if isinstance(raw, str) else {}
        if state.get("send") and not state.get("stop"):
            return True
        time.sleep(1.0)
    return False


def send_message(text: str) -> dict[str, object]:
    """功能：在输入框填入文本并点发送，返回发送前的基线状态。"""
    # 发送前必须等上一轮生成结束：生成中发送键被换成停止键，
    # 强行发送会丢失输入并让本次等待落到上一轮，最终超时。
    # 这里只做短等（上一轮正常您已结束）；预算已在 runner 侧控制，
    # 此处再给 600s 会让一条卡住的用例吞掉后续所有用例的时间。
    if not wait_until_idle(timeout=90):
        return {"ok": False, "reason": "composer_busy"}
    safe = json.dumps(text, ensure_ascii=False)
    script = f"""(() => {{
      // 基线必须是「最后一条助手气泡的文本」，不是气泡数。
      // 产品在同一条气泡内就地更新（正在回复中→正在执行→已完成，
      // 工具轮次反复切换），所以助手气泡计数在整轮内恒不变。
      // 旧写法拿数量做基线→「replies <= base」永远成立→每条都等到超时。
      const allMsgs = [...document.querySelectorAll('[data-testid="chat-message"]')];
      const isAssistant = (el) =>
        Boolean(el.querySelector('[data-testid="assistant-avatar"]')) ||
        (el.className || '').includes('items-start');
      const replies = allMsgs.filter(isAssistant);
      const last = replies.length ? replies[replies.length - 1] : null;
      const before = last ? (last.innerText || '') : "";
      const composer = document.querySelector('[data-testid="composer"]');
      if (!composer) return JSON.stringify({{ ok: false, reason: 'no-composer' }});
      const textarea = composer.tagName === 'TEXTAREA'
        ? composer
        : composer.querySelector('textarea, [contenteditable="true"]');
      if (!textarea) return JSON.stringify({{ ok: false, reason: 'no-textarea' }});
      const text = {safe};
      if (textarea.tagName === 'TEXTAREA') {{
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype, 'value'
        ).set;
        setter.call(textarea, text);
      }} else {{
        textarea.textContent = text;
      }}
      textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
      textarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
      textarea.focus();
      return JSON.stringify({{ ok: true, before }});
    }})()"""
    raw = evaluate(script)
    state = json.loads(raw) if isinstance(raw, str) else {}
    if not state.get("ok"):
        return state

    # 等 React 处理完受控输入再点发送
    time.sleep(0.6)
    click = """(() => {
      const btn = document.querySelector('[data-testid="composer-send"]');
      if (!btn) return JSON.stringify({ ok: false, reason: 'no-send' });
      if (btn.disabled) return JSON.stringify({ ok: false, reason: 'disabled' });
      btn.click();
      return JSON.stringify({ ok: true });
    })()"""
    raw = evaluate(click)
    clicked = json.loads(raw) if isinstance(raw, str) else {}
    state.update(clicked)
    return state


def wait_for_reply(
    before: object, *, timeout: float = SEND_TIMEOUT_SEC
) -> dict[str, object]:
    """功能：等待本轮助手气泡不再变化，并返回最终正文。

    基线形式：`send_message` 返回的「发送前最后一条助手气泡文本」。
    为何不用气泡数：产品在**同一条气泡内**就地更新
    （正在回复中 → 正在执行… → 已完成 · Ns，工具轮次反复切换），
    助手气泡计数在整轮内恒不变；拿数量做基线会让「有新回复」永不成立。
    判定：文本与基线不同 ↔ 产生了本轮内容；send 恢复且不含「正在」↔ 终态。
    """
    deadline = time.time() + timeout
    base_text = before if isinstance(before, str) else ""
    base_count = int(before) if isinstance(before, int) else -1
    answered: list[str] = []
    last_text = ""
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SEC)
        try:
            from card_responder import answer_once, detect_pending

            state = detect_pending()
            needs = bool(
                state.get("card")
                or state.get("permissionOnce")
                or state.get("submit")
                or state.get("execute")
            )
            if needs and len(answered) < MAX_CARD_ANSWERS:
                # 功能：连续清卡直到没卡（实测同一次审批会堆 3+ 张）。
                # 每清一张都 sleep 一轮会把“审批等待”拉长到几十秒，也容易撞时间预算。
                for _ in range(8):
                    action = answer_once()
                    if not action:
                        break
                    answered.append(action)
                    time.sleep(0.25)
                # 不能因为“已答卡”就 continue：卡答完后生成可能已经结束，
                # 跳过气泡检查就会一直等到超时（真机：“你想先推进哪一项”答完后无人发现已完成）。
                time.sleep(0.25)
        except Exception:
            pass

        script = """(() => {
          const msgs = [...document.querySelectorAll('[data-testid="chat-message"]')];
          const roleOf = (el) => {
            // 功能：从 chat-message 自身的布局类刐触互助手，不靠父层。
            // 真机 DOM：message-bubble 的父层是空 className，角色在
            // chat-message 上（items-start / items-end）——旧选择器查错层级，
            // 助手气泡数永远少一，新回复永远被当成上一轮。
            if (el.querySelector('[data-testid="assistant-avatar"]')) return 'A';
            const cls = el.className || '';
            if (cls.includes('items-start')) return 'A';
            if (cls.includes('items-end')) return 'U';
            return 'A';
          };
          const replies = msgs.filter((el) => roleOf(el) === 'A');
          const lastReply = replies.length ? replies[replies.length - 1] : null;
          const blocksInLast = lastReply
            ? lastReply.querySelectorAll('[data-testid="process-block"]').length
            : 0;
          return JSON.stringify({
            count: msgs.length,
            blocksInLast,
            replies: replies.length,
            tail: lastReply ? (lastReply.innerText || '') : '',
            // 正文区长度：过程块不计入回复（产品把思考/工具渲染在
            // process-block，只有 markdown-body 是用户要的答案）
            bodyLen: (() => {
              const md = lastReply ? lastReply.querySelector('[data-testid="markdown-body"]') : null;
              return md ? (md.innerText || '').length : 0;
            })(),
            send: Boolean(document.querySelector('[data-testid="composer-send"]')),
            streaming: Boolean(document.querySelector('[data-testid="streaming-cursor"]'))
          });
        })()"""
        raw = evaluate(script)
        state = json.loads(raw) if isinstance(raw, str) else {}
        replies = int(state.get("replies") or 0)

        text = str(state.get("tail") or "")
        if not text.strip():
            continue
        # 功能：占位态不算回复——用户看到的「正在回复中… 1s」是计时器，
        # 不是模型输出；把它当成回复会让每条都“成功”但内容无效。
        # 必须等到「已完成 · Ns」之后的正文段。
        if "已完成" not in text and "正在" in text:
            last_text = text
            continue
        # 本轮未产生内容：文本仍等于发送前基线（产品尚未动笔）
        if text.strip() == base_text.strip() and replies <= base_count:
            continue
        streaming = bool(state.get("streaming"))
        if streaming:
            last_text = text
            continue
        # 功能：等文本真正稳定后才返回。
        # 产品的「已完成 · Ns」与正文是分步渲染的：状态行先出现，
        # 正文在之后才贴上。旧实现一看到「已完成」就返回，拿到的是只有
        # 状态行的空正文（评测里 10 条“tokens≈7”全是这个原因）。
        # 判定：连续两次轮询文本完全相同 ↔ 已稳定。
        settle_deadline = time.time() + 6.0
        settled = text
        while time.time() < settle_deadline:
            time.sleep(1.0)
            probe = json.loads(evaluate(script) or "{}")
            probe_text = str(probe.get("tail") or "")
            if probe_text == settled:
                break
            settled = probe_text
        if settled.strip():
            last_text = settled
        raw2 = evaluate(script)
        state2 = json.loads(raw2) if isinstance(raw2, str) else {}
        # 功能：生成键消失即本轮已终止（包括被中止）。
        # 断线/中止后界面不会再产生新正文，不认这个信号就会一直等到超时。
        ended = bool(state2.get("send")) and not state2.get("streaming")
        if int(state2.get("replies") or 0) == replies and (not state2.get("streaming") or ended):
            return {
                "ok": True,
                "count": int(state2.get("count") or 0),
                "text": str(state2.get("tail") or text),
                "tool_calls": int(state2.get("blocksInLast") or 0),
                "body_len": int(state2.get("bodyLen") or 0),
            }
        last_text = text
    # 功能：超时后强制停止生成。
    # 不停就永远卡在「生成中」：后续每一条都会因发送键被禁用
    # 而被跳过，一条卡死拖垮整轮。
    try:
        evaluate(
            "(() => { const s = document.querySelector('[data-testid=\"composer-stop\"]');"
            " if (s) { s.click(); return 'stopped'; } return 'no-stop'; })()",
            timeout=15,
        )
        time.sleep(2)
    except Exception:
        pass
    return {"ok": False, "reason": "timeout", "text": last_text}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--send")
    args = parser.parse_args()

    if args.check:
        print(json.dumps(check_ui(), ensure_ascii=False, indent=2))
        return 0

    if not args.send:
        parser.print_help()
        return 1

    sent = send_message(args.send)
    print(f"  发送: {json.dumps(sent, ensure_ascii=False)}")
    if not sent.get("ok"):
        return 1
    reply = wait_for_reply(int(sent.get("before") or 0))
    print(f"  回复: {json.dumps(reply, ensure_ascii=False)[:400]}")
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

def new_session() -> bool:
    """功能：新建一个干净会话并等它就绪。

    用例必须逐条隔离：真机实测同一会话连跑 80 条时，
    第 1-15 条全对，16-24 条连续 LLM 失败，25 条之后大量空回复——
    失败与序号强相关，是会话上下文累积污染（真机见 9.7 万 prompt tokens
    + 36 次 orphan tool message），不是用例本身的问题。
    """
    script = """(() => {
      const btn = [...document.querySelectorAll('button')]
        .find((b) => (b.textContent || '').includes('\u65b0\u5efa\u4f1a\u8bdd'));
      if (!btn) return 'no-btn';
      btn.click();
      return 'clicked';
    })()"""
    try:
        if evaluate(script, timeout=30) != "clicked":
            return False
    except Exception:
        return False
    # 功能：等新会话清空且**稳定**后才返回。
    # 只看一次 n==0 不够：切换瞬间旧会话气泡会被重新读回，
    # 那时 send_message 拿到的 before 就是旧会话的最后一条——
    # wait_for_reply 比对新会话的气泡永远对不上（真机 10 条 bodyLen=0）。
    probe = (
        "(() => JSON.stringify({"
        " n: document.querySelectorAll('[data-testid=\"chat-message\"]').length,"
        " send: Boolean(document.querySelector('[data-testid=\"composer-send\"]')),"
        " stop: Boolean(document.querySelector('[data-testid=\"composer-stop\"]'))"
        "}))()"
    )
    deadline = time.time() + 40
    empty_streak = 0
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            state = json.loads(evaluate(probe, timeout=25) or "{}")
        except Exception:
            empty_streak = 0
            continue
        clean = int(state.get("n") or 0) == 0 and state.get("send") and not state.get("stop")
        empty_streak = empty_streak + 1 if clean else 0
        if empty_streak >= 2:
            return True
    return False
