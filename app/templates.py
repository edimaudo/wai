from fastapi.templating import Jinja2Templates

from app.config import PROJECT_ROOT
from app.i18n import localize_value

templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))
templates.env.filters["localize"] = localize_value
