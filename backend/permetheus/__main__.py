import uvicorn

uvicorn.run("permetheus.app:create_app", factory=True, host="127.0.0.1", port=4311)
