"""GT4 网页滑块助手.

账号风险态/新终端首登时, GetToken → 12114(captchaData)需要 GT4 人机验证。
本模块提供"半自动"闭环:

  1. gt4_helper.build_gt4_html(verify_token, account_enc)
       → 生成 gt4.html(注入 verifyToken, 用户浏览器打开后直接出现滑块)
  2. GT4TupleListener (aiohttp 服务器, 端口 8765)
       → 用户手动滑块 → 校验页面 POST 四元组 → 监听器收到
  3. GT4TupleListener 内部回调 → ImouClient.async_check_geetest4()
       → 10000 → 重发短信(GetValidCode/SMSLogin) → 用户输码 → 闭环

部署形态(HA 插件内, 定稿):
  - async_setup 注册 HomeAssistantView(挂在 HA 自身 8123 端口, 容器零配置)
  - 用户浏览器: http://<ha-host>:8123/api/lechange/gt4/slides (config_flow 生成时缓存)
  - 滑块回传: POST /api/lechange/gt4/tuple (requires_auth=False, 同源无 CORS 问题)
  - 用户无需 phpStudy / 额外端口映射; 反向代理/HTTPS 场景相对路径自动跟随
  - 独立监听形态(start/stop, 8765)保留供调试/无 HA 场景, 容器需 -p 8765:8765

实现注意(实测结论):
  - CheckGeeTest4 身份必须是 default 前缀 + OEM_AK, 用 SK 单哈希密钥(与密码双哈希不同!)
  - usage 必须与 GetValidCode 的 usage 一致(SMSLogin/Login/GrantingCredit)
  - verifyToken 从 12114 响应的 captchaData.verifyToken 取(10min 有效)
  - 四元组 lot_number/captcha_output/pass_token/gen_time 缺一不可
  - 电脑浏览器滑块可行(WebView UA 已注入页面), 设备指纹层差异不影响
    CheckGeeTest4 受理(实测闭环成功)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

from aiohttp import web

from .const import (
    GT4_CAPTCHA_ID,
    GT4_LISTEN_PATH,
    GT4_LISTEN_PORT,
    OEM_AK,
)

_LOGGER = logging.getLogger(__name__)

_GT4_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>乐橙 GT4 人机验证</title>
<script>
// 使用移动端 WebView UA: 与线上客户端四元组产出环境保持一致
Object.defineProperty(navigator, 'userAgent', {
  get: function () { return 'Mozilla/5.0 (Linux; Android 11; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/117.0.0.0 Mobile Safari/537.36'; }
});
</script>
<script src="https://static.geetest.com/v4/gt4.js"></script>
<style>
  body { font-family: sans-serif; background: #f6f7f9; text-align: center; padding-top: 48px; }
  #box { display: inline-block; background: #fff; padding: 24px 32px; border-radius: 12px;
         box-shadow: 0 2px 12px rgba(0,0,0,.08); }
  #status { margin: 12px 0; color: #b45309; }
  #result { margin-top: 12px; color: #16a34a; font-weight: 600; display: none; }
</style>
</head>
<body>
<div id="box">
  <h3>乐橙 GT4 人机验证</h3>
  <div>账号: <b>__ACCOUNT_LABEL__</b></div>
  <div id="captcha"></div>
  <div id="status">正在加载验证组件…</div>
  <div id="result"></div>
</div>
<script>
  var CAPTCHA_ID = "__CAPTCHA_ID__";
  var ENDPOINT = "__ENDPOINT__";
  var VERIFY_TOKEN = "__VERIFY_TOKEN__";
  var USAGE = "__USAGE__";
  var ACCOUNT_ENC = "__ACCOUNT_ENC__";
  var statusEl = document.getElementById("status");
  var resultEl = document.getElementById("result");

  function setStatus(t, c) { statusEl.textContent = t; statusEl.style.color = c || "#b45309"; }

  initGeetest4({
    captchaId: CAPTCHA_ID,
    product: "bind",
    language: "zho",
    riskType: "ai"
  }, function (captcha) {
    captcha.onReady(function () { setStatus("✔ 验证组件就绪, 请完成滑块", "#16a34a"); });
    captcha.onSuccess(function (obj) {
      var res = obj;               // {lot_number, captcha_output, pass_token, gen_time, captcha_id?}
      setStatus("✔ 滑块通过, 回传给 HA …", "#2563eb");
      fetch(ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lot_number: res.lot_number,
          captcha_output: res.captcha_output,
          pass_token: res.pass_token,
          gen_time: res.gen_time,
          captcha_id: res.captcha_id || CAPTCHA_ID,
          verify_token: VERIFY_TOKEN,
          usage: USAGE,
          account_enc: ACCOUNT_ENC
        })
      }).then(function (r) { return r.json(); }).then(function (j) {
        resultEl.style.display = "block";
        resultEl.textContent = j.ok ? "✔ 已回传 HA, 请回到 HA 查看短信/登录状态" : "✉ 回传失败: " + (j.error || "");
        resultEl.style.color = j.ok ? "#16a34a" : "#dc2626";
      }).catch(function (e) {
        resultEl.style.display = "block";
        resultEl.textContent = "✉ 回传异常: " + e;
        resultEl.style.color = "#dc2626";
      });
    });
    captcha.onError(function (e) { setStatus("验证组件错误: " + (e.msg || e.code || ""), "#dc2626"); });
    captcha.appendTo("#captcha");
  });
</script>
</body>
</html>
"""


def build_gt4_html(
    account_label: str,
    verify_token: str,
    endpoint: str,
    usage: str = "SMSLogin",
    account_enc: str = "",
) -> str:
    """Render gt4.html with the current challenge parameters injected."""
    html = _GT4_HTML_TEMPLATE
    html = html.replace("__ACCOUNT_LABEL__", account_label)
    html = html.replace("__CAPTCHA_ID__", GT4_CAPTCHA_ID)
    html = html.replace("__ENDPOINT__", endpoint)
    html = html.replace("__VERIFY_TOKEN__", verify_token or "")
    html = html.replace("__USAGE__", usage)
    html = html.replace("__ACCOUNT_ENC__", account_enc or "")
    return html


class GT4TupleListener:
    """GT4 四元组接收器 — 双部署形态:

    形态A(推荐, HA 插件): HomeAssistantView 挂在 HA 8123 端口
        - 容器部署零额外端口映射, 浏览器直接用现有 HA 地址
        - 页面: GET /api/lechange/gt4/slides?token=...
        - 回传: POST /api/lechange/gt4/tuple (requires_auth=False)
        - 用法: hass.http.register_view(GT4HomeAssistantView(callback))
    形态B(独立进程/调试): aiohttp 独立端口监听
        - 端口 8765(与 HA 8123 不冲突); 容器部署需 -p 8765:8765 映射
        - 用法: await listener.start()

    callback 签名: async def on_tuple(t: dict) -> Any
    """

    def __init__(
        self,
        port: int = GT4_LISTEN_PORT,
        on_tuple: Optional[Callable[[dict], Any]] = None,
    ):
        self.port = port
        self._on_tuple = on_tuple
        self._html_cache: Optional[str] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._task: Optional[asyncio.Task] = None
        self.last_tuple: Optional[dict] = None

    # ------------------------------------------------------------- html
    def html_for(
        self, account_label: str, verify_token: str,
        usage: str = "SMSLogin", account_enc: str = "",
        endpoint: Optional[str] = None,
    ) -> str:
        """Generate + cache the slider page for the current challenge.

        endpoint 默认回传到独立端口; HA 形态A 时由调用方传
        '/api/lechange/gt4/tuple'(相对路径, 浏览器自动带 HA host:port)。
        """
        self._html_cache = build_gt4_html(
            account_label, verify_token,
            endpoint or f"http://<ha-host>:{self.port}{GT4_LISTEN_PATH}",
            usage, account_enc,
        )
        return self._html_cache

    # ------------------------------------------------------------- server(形态B)
    async def start(self) -> None:
        """Start standalone listener (idempotent). 容器部署需端口映射."""
        if self._server is not None:
            return
        app = web.Application()
        app.router.add_get("/gt4.html", self._handle_html)
        app.router.add_get("/", self._handle_html)
        app.router.add_post(GT4_LISTEN_PATH, self._handle_tuple)
        app.router.add_options(GT4_LISTEN_PATH, self._handle_options)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        self._server = runner
        _LOGGER.info("GT4 standalone listener on 0.0.0.0:%s (container: -p %s:%s)", self.port, self.port, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.cleanup()
            self._server = None
            _LOGGER.info("GT4 listener stopped")

    # ------------------------------------------------------------- handlers(共用)
    async def handle_html_request(self, request) -> "web.Response":
        """HTML handler — HA view 与独立监听共用."""
        if not self._html_cache:
            return web.Response(
                text="slider page not prepared yet (call html_for first)",
                status=503, content_type="text/plain",
            )
        return web.Response(text=self._html_cache, content_type="text/html")

    async def handle_tuple_request(self, request) -> "web.Response":
        """Tuple handler — HA view 与独立监听共用."""
        try:
            body = await request.text()
            t = json.loads(body)
        except Exception:
            t = {}
        need = ("lot_number", "captcha_output", "pass_token", "gen_time")
        ok = all(t.get(k) for k in need)
        resp = web.Response(
            text='{"ok": ' + ("true" if ok else "false") + "}",
            content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
        if not ok:
            _LOGGER.warning("GT4 tuple incomplete: %s", body[:120])
            return resp
        t.setdefault("captcha_id", GT4_CAPTCHA_ID)
        t["age_min"] = round((time.time() - int(t["gen_time"])) / 60, 2) if str(t.get("gen_time", "")).isdigit() else None
        self.last_tuple = t
        _LOGGER.info("GT4 tuple received (age=%s min), dispatching callback", t.get("age_min"))
        if self._on_tuple is not None:
            asyncio.get_running_loop().create_task(self._dispatch(t))
        return resp

    async def _dispatch(self, t: dict) -> None:
        try:
            result = self._on_tuple(t)
            if asyncio.iscoroutine(result):
                await result
        except Exception as err:  # noqa: BLE001 - 回调异常不拖垮监听
            _LOGGER.exception("GT4 callback failed: %s", err)

    # ------------------------------------------------------- 形态B内部handler
    async def _handle_html(self, request) -> "web.Response":
        return await self.handle_html_request(request)

    async def _handle_options(self, request) -> "web.Response":
        return web.Response(status=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        })

    async def _handle_tuple(self, request) -> "web.Response":
        return await self.handle_tuple_request(request)


# ---------------------------------------------------------------------------
# 形态A: HomeAssistantView(挂在 HA 8123 端口, 容器零配置)
# ---------------------------------------------------------------------------
def build_ha_views(listener: "GT4TupleListener", html_route: str = "/api/lechange/gt4/slides",
                   tuple_route: str = "/api/lechange/gt4/tuple"):
    """构造 HA 原生 view 类(挂在 8123, 免额外端口; requires_auth=False).

    __init__.py 的 async_setup 里:
        from .gt4_helper import GT4TupleListener, build_ha_views
        listener = GT4TupleListener(on_tuple=_my_callback)
        for view in build_ha_views(listener):
            hass.http.register_view(view)
        listener.html_for(account, verify_token, usage,
                          endpoint='/api/lechange/gt4/tuple')  # 相对路径!
        # 浏览器: http://<ha>:8123/api/lechange/gt4/slides

    说明:
      - requires_auth=False: 滑块页需在手机/任意浏览器匿名打开(短信场景);
        四元组本身不构成安全凭证(单次有效+10min过期), 风险可控。
      - CORS: 同源(页面与回传同在 8123)无需 CORS 头, 但页面以相对路径 fetch
        更稳(反向代理/HTTPS 场景自动跟随)。
    """
    from homeassistant.components.http import HomeAssistantView

    class GT4SlidesView(HomeAssistantView):
        url = html_route
        name = "api:lechange:gt4:slides"
        requires_auth = False

        async def get(self, request):
            return await listener.handle_html_request(request)

    class GT4TupleView(HomeAssistantView):
        url = tuple_route
        name = "api:lechange:gt4:tuple"
        requires_auth = False
        cors_allowed = True

        async def post(self, request):
            return await listener.handle_tuple_request(request)

    return [GT4SlidesView(), GT4TupleView()]


def parse_verify_token(captcha_data: dict) -> str:
    """Extract verifyToken from a 12114 response data.captchaData."""
    return (captcha_data or {}).get("verifyToken") or ""


def parse_12114(err_code: int, data: dict) -> Optional[dict]:
    """Recognize a 12114 GT4 challenge; return captchaData (may be empty dict)."""
    if err_code != 12114:
        return None
    return (data or {}).get("captchaData") or {}
