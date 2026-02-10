# wsgi.py
from flask import Flask
from routes import setup_routes

app = Flask(__name__)
setup_routes(app)
