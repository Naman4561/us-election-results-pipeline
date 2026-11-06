"""Read-only local interface for the research catalog."""

from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from election_pipeline.research.catalog import Catalog
from election_pipeline.research.metadata import GEOGRAPHIES, KINDS, STATUSES
from election_pipeline.research.query import Query, explore

STATIC = Path(__file__).with_name("static")
VersionId = Annotated[int, PathParam(ge=1, le=2**53 - 1)]


def create_app(root: Path) -> FastAPI:
    catalog = Catalog(root)
    app = FastAPI(title="Election Data Sandbox", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/catalog")
    def versions():
        return {
            "versions": catalog.versions(),
            "kinds": KINDS,
            "statuses": STATUSES,
            "geographies": GEOGRAPHIES,
        }

    @app.get("/api/versions/{version_id}")
    def version(version_id: VersionId):
        try:
            return catalog.version(version_id)
        except KeyError as error:
            raise HTTPException(404, "Unknown dataset version") from error

    @app.post("/api/versions/{version_id}/query")
    def query(version_id: VersionId, request: Query):
        try:
            return explore(catalog, version_id, request)
        except KeyError as error:
            raise HTTPException(404, "Unknown dataset version") from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/api/versions/{version_id}/files/{position}")
    def download(version_id: VersionId, position: Annotated[int, PathParam(ge=0, le=1000)]):
        try:
            file_id, filename, size = catalog.file(version_id, position)
        except KeyError as error:
            raise HTTPException(404, "Unknown file") from error
        return StreamingResponse(
            catalog.file_chunks(file_id),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                "Content-Length": str(size),
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app
