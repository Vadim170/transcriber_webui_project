from app import create_app

app = create_app()

if __name__ == "__main__":
    cfg = app.config["APP_CFG"]
    host = cfg["host"]
    port = cfg["port"]
    print(f"Server: http://{host}:{port}")
    if cfg.get("_generated_password"):
        print(f"Password: {cfg['_generated_password']}")
    app.run(host=host, port=port, debug=False, threaded=True)
