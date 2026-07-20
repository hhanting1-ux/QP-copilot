"""
browser.py
负责与浏览器建立连接（或启动新浏览器），返回目标 QP 平台页面的 Page 对象。

切帧操作在 navigator.py 里（FrameNavigator），本模块只管连接/断开浏览器。

设计原则：
- 只读操作：本模块不会点击"合格/驳回/提交"等按钮。
- 两种连接模式（由 config.yaml 的 browser.mode 决定）：
  1) cdp    : 连接用户手动打开、已登录好 QP 平台的 Chrome（通过 --remote-debugging-port）
  2) launch : 脚本自己启动一个新的 Chrome 窗口，用户需要在新窗口里手动登录
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright, Playwright, Browser, BrowserContext, Page


class BrowserSession:
    """封装一次浏览器会话：负责连接/启动浏览器，并提供翻页方法。"""

    def __init__(self, config: dict):
        self.config = config
        self._playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    # ------------------------------------------------------------------
    # 连接 / 启动
    # ------------------------------------------------------------------
    def start(self) -> Page:
        """根据配置连接已有浏览器或启动新浏览器，返回目标页面（Page）对象。"""
        self._playwright = sync_playwright().start()
        mode = self.config["browser"]["mode"]

        if mode == "cdp":
            self.page = self._connect_via_cdp()
        elif mode == "launch":
            self.page = self._launch_new_browser()
        else:
            raise ValueError(f"未知的 browser.mode: {mode}，请使用 'cdp' 或 'launch'")

        return self.page

    def _connect_via_cdp(self) -> Page:
        """连接用户已经手动打开并登录好的 Chrome（推荐方式，账号密码全程不经过脚本）。"""
        cdp_url = self.config["browser"]["cdp_url"]
        try:
            self.browser = self._playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise RuntimeError(
                f"无法连接到 {cdp_url}。\n"
                "请确认你已经按 README 中的说明，用调试模式启动了 Chrome，"
                "并且已经手动登录 QP 平台、打开了目标 scene 页面。\n"
                f"原始错误: {exc}"
            ) from exc

        # CDP 连接下浏览器可能有多个 context / 多个 tab，找到目标页面
        contexts = self.browser.contexts
        if not contexts:
            raise RuntimeError("连接成功，但没有找到任何浏览器 context/标签页。")
        self.context = contexts[0]

        page = self._pick_target_page(self.context)
        return page

    def _pick_target_page(self, context: BrowserContext) -> Page:
        """在当前 context 的所有标签页中，按标题关键字挑选目标 QP 页面。"""
        hint = (self.config["browser"].get("page_title_hint") or "").strip()
        pages = context.pages
        if not pages:
            raise RuntimeError("当前浏览器 context 没有任何打开的标签页。")

        if not hint:
            return pages[0]

        for p in pages:
            try:
                if hint in p.title():
                    return p
            except Exception:
                continue

        print(f"[警告] 没有标签页标题包含 '{hint}'，将使用第一个标签页。")
        return pages[0]

    def _launch_new_browser(self) -> Page:
        """脚本自己启动一个新的浏览器窗口，用户需要在里面手动登录 QP 平台。"""
        launch_cfg = self.config["browser"]["launch"]
        self.browser = self._playwright.chromium.launch(
            headless=launch_cfg.get("headless", False),
            channel=launch_cfg.get("channel") or None,
            slow_mo=launch_cfg.get("slow_mo_ms", 0),
        )
        self.context = self.browser.new_context()
        page = self.context.new_page()
        return page

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    def close(self, close_browser: bool = False) -> None:
        """结束会话。默认不关闭浏览器本身（CDP 模式下浏览器是用户的，不应由脚本关闭）。"""
        if close_browser and self.browser:
            self.browser.close()
        if self._playwright:
            self._playwright.stop()
