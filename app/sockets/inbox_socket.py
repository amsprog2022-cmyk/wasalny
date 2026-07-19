"""WebSocket namespace for the inbox: pushes new messages to all connected clients."""
from flask_login import current_user
from flask_socketio import join_room, disconnect

from app import socketio


@socketio.on("connect", namespace="/inbox")
def on_connect():
    if not current_user.is_authenticated:
        return disconnect()
    join_room(f"user:{current_user.id}")


@socketio.on("disconnect", namespace="/inbox")
def on_disconnect(reason=None):
    # reason arg added in python-socketio 5.12+.
    pass
