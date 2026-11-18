"""本机 JSON API；不提供 HTML，也不知道界面的布局。"""
import argparse
import http.client
import json
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from session_layer import dispatch


def installed_models():
    # 只向本机 Ollama 查询清单，不下载模型，也不接受浏览器指定服务地址。
    with closing(http.client.HTTPConnection('127.0.0.1', 11434, timeout=4)) as conn:
        conn.request('GET', '/api/tags')
        reply = conn.getresponse()
        if reply.status != 200:
            raise OSError(f'Ollama HTTP {reply.status}')
        return [item['name'] for item in json.load(reply)['models']]


class Handler(BaseHTTPRequestHandler):
    ui_origin = 'http://127.0.0.1:8766'

    def end_headers(self):
        if self.headers.get('Origin') == self.ui_origin:
            self.send_header('Access-Control-Allow-Origin', self.ui_origin)
            self.send_header('Vary', 'Origin')
        super().end_headers()

    def do_OPTIONS(self):
        if not self.local():
            return self.send({}, 403)
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def send(self, data, status=200, mime='application/json; charset=utf-8'):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def local(self):
        host = f'127.0.0.1:{self.server.server_port}'
        return self.headers.get('Host') == host and self.headers.get('Origin') in (None, self.ui_origin)

    def do_GET(self):
        if not self.local():
            return self.send({}, 403)
        if self.path == '/info':
            root = self.server.root
            return self.send(dict(root=str(root), workspaces=sorted(p.name for p in root.iterdir()
                if not p.is_symlink() and (p / '.mini-harness').is_dir())))
        if self.path == '/models':
            try:
                return self.send(dict(models=installed_models(), default_model=self.server.default_model))
            except (OSError, http.client.HTTPException, KeyError, ValueError, TypeError) as error:
                return self.send(dict(error=f'无法读取 Ollama 模型列表，请检查本机服务：{error}'), 502)
        self.send({}, 404)

    def do_POST(self):
        if not self.local():
            return self.send({}, 403)
        if self.path not in ('/chat', '/approve', '/session', '/sessions'):
            return self.send({}, 404)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if self.headers.get_content_type() != 'application/json' or not 0 < size <= 128000:
                raise ValueError('请求格式或大小不正确')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('需要 JSON 对象')
            selected = self.server.default_model
            if self.path == '/chat' and 'model' in body:
                selected = body['model']
                if not isinstance(selected, str) or selected not in installed_models():
                    raise ValueError('所选模型不在 Ollama 列表中，请刷新模型列表')
            self.send(dispatch(self.server.root, self.path, body, self.server.model, model_name=selected))
        except ValueError as error:
            self.send(dict(error=str(error)), 400)
        except (OSError, KeyError, http.client.HTTPException) as error:
            self.send(dict(error=f'本地模型或工作目录异常：{error}'), 502)


if __name__ == '__main__':
    from cli import chat
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--workspace', type=Path, default=Path('.mini-harness/workspaces'))
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    with HTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.root = args.workspace.expanduser().resolve()
        server.root.mkdir(parents=True, exist_ok=True)
        server.default_model = args.model
        server.model = chat
        print(f'API：http://127.0.0.1:{server.server_port}   工作目录：{server.root}', flush=True)
        server.serve_forever()
