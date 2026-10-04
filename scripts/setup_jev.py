#!/usr/bin/env python3
"""Configure Jev credentials through a hidden local prompt, never CLI arguments."""
import argparse
import getpass
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib import error, request

CONFIG_HOME = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
PRIVATE_DIR = CONFIG_HOME / 'orca-model-routing'
CREDENTIALS = PRIVATE_DIR / 'jev-credentials.json'
ENDPOINTS = {
    'typesafe': 'https://api.typesafe.ai/v1/models',
    'openrouter': 'https://openrouter.ai/api/v1/key',
}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify(provider, key):
    req = request.Request(ENDPOINTS[provider], headers={
        'Authorization': 'Bearer ' + key,
        'Accept': 'application/json',
        'User-Agent': 'orca-model-routing-key-setup/1',
    })
    # No inference call, workspace content, or credentials in URLs or logs.
    opener = request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=20) as response:
            data = json.loads(response.read(131073))
    except error.HTTPError as exc:
        raise ValueError('认证检查返回 HTTP %s；未显示服务端正文。' % exc.code) from None
    except (error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise ValueError('认证检查连接失败或响应格式异常；未显示任何凭据。') from None
    if provider == 'typesafe':
        names = [m.get('name', '') for m in data.get('models', []) if isinstance(m, dict)]
        if not any(n.startswith('jev') for n in names):
            raise ValueError('模型目录未返回 Jev；不能确认该 Key 有可用的 Jev 访问。')
        return {'authentication_verified': True, 'jev_catalog_available': True}
    if not isinstance(data.get('data'), dict):
        raise ValueError('API Key 检查响应异常。')
    if data['data'].get('is_management_key') or data['data'].get('is_provisioning_key'):
        raise ValueError('这是管理 Key，请使用可调用模型的普通 API Key。')
    return {'authentication_verified': True, 'jev_inference_verified': False}


def save_private(path, value):
    PRIVATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if PRIVATE_DIR.is_symlink():
        raise ValueError('凭据目录是符号链接，停止写入。')
    os.chmod(PRIVATE_DIR, 0o700)
    fd, temp = tempfile.mkstemp(prefix='.jev-', dir=PRIVATE_DIR)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Recheck saved credentials without printing them')
    args = parser.parse_args()
    if args.check:
        if not CREDENTIALS.is_file():
            print('尚未保存 Jev API Key。')
            return 1
        stored = json.loads(CREDENTIALS.read_text())
        provider, key = stored['provider'], stored['api_key']
    else:
        if not sys.stdin.isatty():
            print('请在本机交互终端运行；拒绝回显或通过参数接收 Key。')
            return 2
        print('Jev API Key 配置（仅配置凭据，不启用自动路由）')
        print('1 = TypeSafe 官方 Key；2 = OpenRouter Key')
        selected = input('请选择来源 [默认 1]：').strip() or '1'
        if selected not in ['1', '2']:
            print('未选择有效来源，配置已取消。')
            return 2
        provider = {'1': 'typesafe', '2': 'openrouter'}[selected]
        key = getpass.getpass('粘贴 API Key 后回车（输入隐藏）：').strip()
        if not key or any(c.isspace() for c in key):
            print('Key 为空或含空白，未保存。')
            return 2
    try:
        evidence = verify(provider, key)
        if not args.check:
            save_private(CREDENTIALS, {'provider': provider, 'api_key': key})
        print('API Key 已通过认证检查，并保存到本机用户私有目录（文件权限 0600）。')
        print('来源：' + provider + '；Jev 自动路由是否启用仍由本地路由配置决定。')
        print('这次没有调用 Jev 推理，也没有发送项目内容。')
        return 0
    except ValueError as exc:
        print(str(exc))
        print('未写入新的 Key，也未启用路由。')
        return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\n配置已取消。')
        sys.exit(130)
