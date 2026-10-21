import uvicorn


def main() -> None:
    uvicorn.run("lumewarp_control_plane.app:create_app", factory=True, host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
