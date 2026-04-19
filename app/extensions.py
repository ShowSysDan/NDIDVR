from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from apscheduler.schedulers.background import BackgroundScheduler

db = SQLAlchemy()
socketio = SocketIO()          # async_mode set in create_app / wsgi.py
scheduler = BackgroundScheduler(timezone="UTC")
