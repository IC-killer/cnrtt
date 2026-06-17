"""cnrtt 包的冒烟测试：仅验证可导入、入口函数存在。"""

import cnrtt


def test_version():
    assert isinstance(cnrtt.__version__, str)
    assert cnrtt.__version__ == "0.1.0"


def test_main_callable():
    assert callable(cnrtt.main)


def test_app_class_importable():
    assert hasattr(cnrtt, "RTTViewerApp")
