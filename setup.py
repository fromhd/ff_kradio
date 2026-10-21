from plugin import create_plugin_instance

setting = {
    "filepath": __file__,
    "use_db": True,
    "use_default_setting": True,
    "home_module": "setting",
    "menu": {
        "uri": __package__,
        "name": "K-Radio",
        "list": [
            {"uri": "setting", "name": "채널 관리"},
            {"uri": "json_view", "name": "JSON 에디터"},
            {"uri": "log", "name": "로그"},
        ],
    },
    "setting_menu": None,
    "default_route": "single",
}

P = create_plugin_instance(setting)
from .logic import Logic
P.set_module_list([Logic])
P.logic.plugin_load = P.module_list[0].plugin_load
