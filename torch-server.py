from http.server import BaseHTTPRequestHandler, HTTPServer
import subprocess

PORT = 8282

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if '/torch/on' in self.path:
            subprocess.Popen(['termux-torch', 'on'])
            self._respond('on')
        elif '/torch/off' in self.path:
            subprocess.Popen(['termux-torch', 'off'])
            self._respond('off')
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, state):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(f'{{"torch": "{state}"}}'.encode())

    def log_message(self, *args):
        pass

if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Torch server running on port {PORT}')
    server.serve_forever()
