import logging, os, time, uuid
from logging.handlers import RotatingFileHandler
from flask import g, request

def init_logging(app):
    log_dir = os.path.join(app.root_path, "logs")
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(log_dir, "gijiro.log"),
                                  maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s :: %(message)s"))
    handler.setLevel(logging.INFO)

    app.logger.setLevel(logging.INFO)
    app.logger.addHandler(handler)

    @app.before_request
    def _before():
        g.req_id = str(uuid.uuid4())[:8]
        g.t0 = time.time()
        app.logger.info(f"[{g.req_id}] ⇢ {request.method} {request.path}")

    @app.after_request
    def _after(resp):
        dt = time.time() - getattr(g, "t0", time.time())
        resp.headers["X-Request-Id"] = getattr(g, "req_id", "")
        app.logger.info(f"[{getattr(g,'req_id','-')}] ⇠ {resp.status_code} {request.method} {request.path} ({dt:.3f}s)")
        return resp
