from flask import Flask, request, render_template, redirect, url_for, flash, session, jsonify
from flask_socketio import SocketIO, emit, join_room
from datetime import datetime
from threading import Lock
import sqlite3
import os

app = Flask(__name__, template_folder="templates")
app.secret_key = "supersecretkey"  # replace in production

socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

clients = {}    # username -> connected?
lock = Lock()

DB_FILE = "chat.db"


# -------------------- Database helpers --------------------
def _connect():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = _connect()
    c = conn.cursor()

    # Users table
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    # Messages table
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT,               -- NULL => group/broadcast
            message TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

def register_user(username, password):
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
        conn.commit()
        user_id = c.lastrowid
        return user_id
    except sqlite3.IntegrityError:
        conn.close()
        return None
    finally:
        conn.close()

def get_user(username):
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT id, username, password FROM users WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()
    return row  # (id, username, password) or None

def save_message(sender, recipient, message, ts):
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (sender, recipient, message, timestamp) VALUES (?, ?, ?, ?)",
        (sender, recipient, message, ts)
    )
    conn.commit()
    conn.close()

def get_group_messages():
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        SELECT sender, recipient, message, timestamp
        FROM messages
        WHERE recipient IS NULL
        ORDER BY id ASC
    """)
    msgs = c.fetchall()
    conn.close()
    return msgs

def get_private_messages(user1, user2):
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        SELECT sender, recipient, message, timestamp
        FROM messages
        WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?)
        ORDER BY id ASC
    """, (user1, user2, user2, user1))
    msgs = c.fetchall()
    conn.close()
    return msgs


# -------------------- Routes --------------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if not username or not password:
            flash("Both fields required.", "error")
            return redirect(url_for("login"))

        with lock:
            user = get_user(username)
            if user:
                user_id, db_username, db_password = user
                if db_password == password:
                    session["username"] = username
                    flash("Login successful!", "success")
                    return redirect(url_for("chat"))
                else:
                    flash("Invalid password.", "error")
                    return redirect(url_for("login"))
            else:
                user_id = register_user(username, password)
                if user_id:
                    session["username"] = username
                    flash("Registered successfully!", "success")
                    return redirect(url_for("chat"))
                else:
                    flash("Username already exists.", "error")
                    return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/chat")
def chat():
    if "username" not in session:
        flash("Please log in.", "error")
        return redirect(url_for("login"))
    return render_template("chat.html", username=session["username"])


@app.route("/logout")
def logout():
    if "username" in session:
        uname = session["username"]
        with lock:
            clients.pop(uname, None)
        session.pop("username")
        socketio.emit("user_status", {"users": list(clients.keys())})
    return redirect(url_for("login"))


# ----- History APIs -----
@app.route("/history/group")
def history_group():
    if "username" not in session:
        return jsonify({"error": "Not logged in"}), 403
    msgs = get_group_messages()
    return jsonify([
        {"sender": m[0], "recipient": m[1], "message": m[2], "timestamp": m[3]}
        for m in msgs
    ])

@app.route("/history/<other_user>")
def history_private(other_user):
    if "username" not in session:
        return jsonify({"error": "Not logged in"}), 403
    me = session["username"]
    msgs = get_private_messages(me, other_user)
    return jsonify([
        {"sender": m[0], "recipient": m[1], "message": m[2], "timestamp": m[3]}
        for m in msgs
    ])


# -------------------- Socket.IO events --------------------
@socketio.on("connect")
def handle_connect(auth=None):
    uname = None
    if auth and "username" in auth:
        uname = auth["username"]
    elif "username" in session:
        uname = session["username"]

    if uname:
        session["username"] = uname
        join_room(uname)
        with lock:
            clients[uname] = True
        socketio.emit("user_status", {"users": list(clients.keys())})
        print(f"[CONNECT] {uname} connected.")


@socketio.on("disconnect")
def handle_disconnect(reason=None):
    uname = session.get("username")
    if uname:
        with lock:
            clients.pop(uname, None)
        socketio.emit("user_status", {"users": list(clients.keys())})
        print(f"[DISCONNECT] {uname} disconnected.")


@socketio.on("send_message")
def handle_send_message(data):
    sender = session.get("username")
    recipient = data.get("recipient")
    message = (data.get("message") or "").strip()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not sender or not message:
        return

    if recipient == "":
        recipient = None

    save_message(sender, recipient, message, ts)

    if recipient:
        emit("new_message", {"sender": sender, "recipient": recipient, "message": message, "timestamp": ts}, room=recipient)
        emit("new_message", {"sender": sender, "recipient": recipient, "message": message, "timestamp": ts}, room=sender)
    else:
        for user in clients:
            emit("new_message", {"sender": sender, "recipient": None, "message": message, "timestamp": ts}, room=user)


# -------------------- Run --------------------
if __name__ == "__main__":
    os.makedirs("templates", exist_ok=True)
    init_db()

    import eventlet
    import eventlet.wsgi

    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
