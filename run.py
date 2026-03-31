from app import create_app, socketio

app = create_app()

if __name__ == "__main__":
    cfg = app.config["APP_CFG"]
    host = cfg["host"]
    port = cfg["port"]
    print(f"Server: http://{host}:{port}")
    if cfg.get("_generated_password"):
        print(f"Password: {cfg['_generated_password']}")
    print("WARNING: The built-in Flask server is for local / development use only.")
    print("         For network or production use: gunicorn -w 1 \"run:app\"")
    socketio.run(app, host=host, port=port, debug=False)
