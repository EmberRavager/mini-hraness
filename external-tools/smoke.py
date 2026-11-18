"""从临时会话目录验收已安装的 MCP 工具。加 --online 发出一次公开查询。"""
import argparse
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tool_registry import DEFAULT_CONFIG, artifacts, connect_tools


def check(online):
    html = '<!doctype html><meta charset="utf-8"><title>工具验收</title><h1>你好</h1>'
    state = dict(files={}, online=online, deadline=time.time() + 60)
    with TemporaryDirectory() as directory:
        folder = Path(directory)
        with connect_tools(DEFAULT_CONFIG, folder, state) as tools:
            expected = {'list_directory', 'read_text_file', 'write_file'}
            if online:
                expected.add('search')
            assert set(tools.schemas) == expected, tools.schemas.keys()
            assert set(tools.read_only) == {'list_directory', 'read_text_file'}
            call = tools.functions
            call['write_file'](path='index.html', content=html)
            assert html == call['read_text_file'](path='index.html')
            assert 'index.html' in call['list_directory'](path='.')
            assert artifacts(folder) == {'index.html': html}
            print('PASS  已写入、列出、读回中文网页，并核对可下载产物。')
            if online:
                result = json.loads(call['search'](query='Python official website', limit=3))
                links = [r['url'] for r in result['results'] if r['url'].startswith(('https://', 'http://'))]
                assert links, result.get('partialFailures')
                print('PASS  公开搜索返回', len(links), '条链接。', links[0])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true')
    check(parser.parse_args().online)
