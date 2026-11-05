from fastapi.templating import Jinja2Templates

from app.config import PROJECT_ROOT

templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))
