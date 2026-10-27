"""本地交互获取 Strava OAuth refresh token；不要在 Actions 中运行。"""

from __future__ import annotations

import getpass
import secrets
import subprocess
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import requests


def parse_redirect(value: str, expected_state: str) -> str:
    url = urlparse(value.strip())
    if (
        url.scheme != "http"
        or url.hostname != "localhost"
        or url.path != "/strava-callback"
    ):
        raise ValueError("请粘贴授权后地址栏中的完整 localhost 回调 URL")
    query = parse_qs(url.query)
    if query.get("state") != [expected_state]:
        raise ValueError("授权 state 不匹配，请使用本次生成的授权链接")
    if query.get("error"):
        raise ValueError("Strava 授权被取消，请重新运行并允许读取全部活动和写入活动")
    scopes = set(query.get("scope", [""])[0].replace(" ", ",").split(","))
    missing = {"activity:read_all", "activity:write"} - scopes
    if missing:
        raise ValueError(f"缺少 {', '.join(sorted(missing))} 权限，请重新授权")
    code = query.get("code", [""])[0]
    if not code:
        raise ValueError("回调 URL 中缺少授权 code")
    return code


def main(*, upgrade: bool = False) -> int:
    if upgrade:
        print("这是已有 Strava 同步的重新授权工具，不需要输入或记住任何命令。")
        print("完成后只更新 GitHub 中的 STRAVA_REFRESH_TOKEN；保留原会话密钥和台账。")
        print("Client ID 和 Client Secret 在 Strava 的“我的 API 应用”页面里。")
        print("隐藏输入时，粘贴内容不会显示出来，粘贴后直接按回车即可。\n")
    print(
        "请先在 Strava API 应用设置中把 Authorization Callback Domain 设为 localhost。"
    )
    client_id = input("Strava Client ID: ").strip()
    if not client_id.isascii() or not client_id.isdigit():
        print("Client ID 必须是数字")
        return 1
    client_secret = getpass.getpass("Strava Client Secret（输入隐藏）: ").strip()
    state = secrets.token_urlsafe(32)
    print("在浏览器打开以下链接，登录需要同步的 Strava 账号并允许读取全部活动和写入活动：")
    print(
        "https://www.strava.com/oauth/authorize?"
        + urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": "http://localhost/strava-callback",
                "approval_prompt": "force",
                "scope": "activity:read_all,activity:write",
                "state": state,
            }
        )
    )
    print("授权后 localhost 页面可能无法打开，这是正常的；复制地址栏完整 URL。")
    try:
        code = parse_redirect(getpass.getpass("回调 URL（输入隐藏）: "), state)
        response = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=(15, 45),
            allow_redirects=False,
        )
        if response.status_code != 200:
            print(
                f"换取授权失败（HTTP {response.status_code}），请检查应用配置并重新授权。"
            )
            return 1
        payload = response.json()
        token = payload.get("refresh_token")
        if not isinstance(token, str) or not token:
            print("Strava 未返回 refresh token，请重新授权。")
            return 1
    except (ValueError, requests.RequestException) as exc:
        print(
            str(exc)
            if isinstance(exc, ValueError)
            else "Strava 网络请求失败，请稍后重试。"
        )
        return 1
    if upgrade:
        print("\n授权成功！下一步只需在 GitHub 更新一个配置：")
        print("仓库 Settings → Secrets and variables → Actions → Secrets")
        print("找到 STRAVA_REFRESH_TOKEN，点击旁边的铅笔，替换后点击 Update secret。")
        if sys.platform == "darwin":
            try:
                subprocess.run(["pbcopy"], input=token, text=True, check=True)
                print("新授权值已复制，可以直接在 GitHub 的输入框里粘贴。")
            except (OSError, subprocess.CalledProcessError):
                print("请手动复制下一行等号后面的完整内容，粘贴到 GitHub。")
        else:
            print("请复制下一行等号后面的完整内容，粘贴到 GitHub。")
        print(f"STRAVA_REFRESH_TOKEN={token}")
        print("不用更改 STRAVA_SESSION_KEY，也不用删除或替换 sync.db。")
        print("保存后，回到仓库 Actions 页面，手动运行一次同步即可。")
    else:
        print("将下面值保存到仓库 Actions Secret；不要提交到代码或公开分享终端输出：")
        print(f"STRAVA_REFRESH_TOKEN={token}")
        print(f"STRAVA_SESSION_KEY={secrets.token_urlsafe(48)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
