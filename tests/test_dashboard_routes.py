import ast
from pathlib import Path
import os
import tempfile
import unittest
from fastapi.responses import HTMLResponse

ROOT=Path(__file__).resolve().parents[1]

class DashboardRoutes(unittest.TestCase):
    def test_methodology_uses_same_live_shell_and_no_cache_headers(self):
        tree=ast.parse((ROOT/'web/app.py').read_text())
        selected=[]
        for node in tree.body:
            if isinstance(node,ast.FunctionDef) and node.name in ('get_index','get_methodology'):
                node.decorator_list=[]
                selected.append(node)
        namespace={'HTMLResponse':HTMLResponse,'__file__':str(ROOT/'web/app.py')}
        exec(compile(ast.Module(body=selected,type_ignores=[]),'routes','exec'),namespace)
        previous=os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                os.chdir(directory)
                Path('index.html').write_text('<html>live dashboard shell</html>')
                response=namespace['get_methodology']()
                self.assertEqual(response.body,b'<html>live dashboard shell</html>')
                self.assertIn('no-store',response.headers['cache-control'])
        finally:
            os.chdir(previous)
