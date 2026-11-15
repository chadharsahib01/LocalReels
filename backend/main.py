from fastapi.responses import FileResponse

@app.get("/")
def reels_ui():
    return FileResponse("reels.html")