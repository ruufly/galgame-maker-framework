"""回归测试: 覆盖历史修复, 防止问题复发。

运行::

    py -3.10 framework/tests/regression_test.py

覆盖:
    1. UI 交互音效 (_play_ui_sound 有真实实现且能触发音频)
    2. 表达式求值: 含括号/点号的字符串不被误判, 函数调用/属性访问仍被拒绝
    3. show <角色> <立绘> [at pos] [with 效果] 保留立绘姿态
    4. 插件隔离: 类形式插件装饰器注册/卸载零残留 + sys.modules 清理
    5. 动态 voice:<角色> 设置项持久化 (save/load)
"""

import os
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pygame  # noqa: E402

from framework.engine.parser import parse, Statement  # noqa: E402
from framework import GameEngine  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def make_engine():
    return GameEngine(320, 240, "t", fps=60)


def test_ui_sound():
    print("== UI 交互音效 ==")
    import inspect
    e = make_engine()
    src = inspect.getsource(e._play_ui_sound)
    check("_play_ui_sound 有真实实现", "resolve_sound" in src)
    check("_play_ui_sound 默认不报错", e._play_ui_sound() is None)
    check("_play_ui_sound('hover') 不报错", e._play_ui_sound("hover") is None)
    # 配置了声音名但文件缺失 -> 不应抛异常 (降级为日志)
    e.ui_click_sound = "no_such_click"
    try:
        e._play_ui_sound()
        check("缺失音效文件不抛异常", True)
    except Exception as exc:
        check(f"缺失音效文件不抛异常 ({exc})", False)
    pygame.quit()


def test_evaluate():
    print("== 表达式求值 ==")
    e = make_engine()
    rt = e.runtime
    check("含括号字符串", rt.evaluate("'hello (world)'") == "hello (world)")
    check("含点号字符串", rt.evaluate("'a.b.c'") == "a.b.c")
    check("逗号字符串比较", rt.evaluate("'a,b' == 'a,b'") is True)
    check("set 解析含括号字符串",
          rt.evaluate(parse('set note = "text (with) parens"')
                      .statements[0].args[1]) == "text (with) parens")
    check("普通算术", rt.evaluate("1 + 2 * 3") == 7)
    e.set_var("love", 5)
    check("变量引用", rt.evaluate("love > 0 and love < 10") is True)
    check("$var 翻译", rt.evaluate("$love + 1") == 6)
    e.set_var("main::x", 2)
    check("命名空间变量", rt.evaluate("$main::x == 2") is True)
    for bad in ("len('x')", "x.__class__", "[1,2].append(3)",
                "__import__('os')"):
        try:
            rt.evaluate(bad)
            check(f"拒绝非法表达式 {bad!r}", False)
        except Exception:
            check(f"拒绝非法表达式 {bad!r}", True)
    pygame.quit()


def test_show_pose_with_effect():
    print("== show 立绘姿态 ==")
    e = make_engine()
    rt = e.runtime
    rt.characters["a"] = {"name": "A",
                          "sprites": {"happy": "h.png", "normal": "n.png"},
                          "default": "n.png", "pos": "center", "scale": None,
                          "mode": None, "voice_volume": 1.0, "meta": {}}
    calls = []

    class FakeDisplay:
        sprites = {}

        def show_sprite(self, *a, **k):
            calls.append((a, k))

    old_display = rt.engine.display
    rt.engine.display = FakeDisplay()
    try:
        calls.clear()
        rt._cmd_show(Statement(op="show", args=["a", "happy", "with", "fade"],
                               line=1))
        check("show ... with 效果保留姿态",
              bool(calls) and calls[0][0][1] == "h.png")
        calls.clear()
        rt._cmd_show(Statement(op="show", args=["a", "happy", "at", "left",
                                                "with", "fade"], line=2))
        check("show ... at ... with 保留姿态",
              bool(calls) and calls[0][0][1] == "h.png")
        calls.clear()
        rt._cmd_show(Statement(op="show", args=["a", "with", "fade"], line=3))
        check("show ... with 无姿态用默认",
              bool(calls) and calls[0][0][1] == "n.png")
    finally:
        rt.engine.display = old_display
        pygame.quit()


def test_plugin_isolation():
    print("== 插件隔离 ==")
    e = make_engine()
    from framework.api.plugin import PluginManager
    plug_dir = tempfile.mkdtemp()
    with open(os.path.join(plug_dir, "iso_plug.py"), "w",
              encoding="utf-8") as f:
        f.write(
            "from framework.api import Plugin, event_listener, command\n"
            "@event_listener('engine_start')\n"
            "def mod_evt(engine, **kw):\n"
            "    pass\n"
            "@command('mod_cmd')\n"
            "def mod_cmd(engine, stmt, **kw):\n"
            "    pass\n"
            "class MyPlugin(Plugin):\n"
            "    name = 'iso_plug'\n"
            "    def on_load(self):\n"
            "        @self.listen('text_show')\n"
            "        def inst_evt(text, **kw):\n"
            "            pass\n"
            "        @self.add_command('inst_cmd')\n"
            "        def inst_cmd(engine, stmt, **kw):\n"
            "            pass\n"
            "    @event_listener('bg_change')\n"
            "    def deco_evt(self, **kw):\n"
            "        pass\n"
            "    @command('deco_cmd')\n"
            "    def deco_cmd(self, engine, stmt, **kw):\n"
            "        pass\n"
        )
    pm = PluginManager(e)
    pm.discover(plug_dir)
    cmds = e.commands
    evs = e.events
    check("模块级指令注册", cmds.has("mod_cmd", "iso_plug"))
    check("实例 listen 指令注册", cmds.has("inst_cmd", "iso_plug"))
    check("装饰器实例指令命名空间正确",
          cmds.has("deco_cmd", "iso_plug") and not cmds.has("deco_cmd",
                                                            "main"))
    check("模块级事件注册", evs.has_handlers("engine_start"))
    check("装饰器实例事件注册", evs.has_handlers("bg_change"))
    # 卸载
    pm.unload_module("gm_plugin_iso_plug")
    check("卸载后模块级指令清除", not cmds.has("mod_cmd"))
    check("卸载后实例指令清除", not cmds.has("inst_cmd"))
    check("卸载后装饰器指令清除", not cmds.has("deco_cmd"))
    check("卸载后模块级事件清除", not evs.has_handlers("engine_start"))
    check("卸载后装饰器实例事件清除", not evs.has_handlers("bg_change"))
    check("sys.modules 无残留", "gm_plugin_iso_plug" not in sys.modules)
    check("实例注册追踪无残留", not pm._inst_regs)
    check("模块注册追踪无残留", not pm._mod_regs)
    check("main 域未被污染", not cmds.has("deco_cmd", "main"))
    # 重载 (文件仍在) 后再次卸载, 不残留
    pm.discover(plug_dir)
    check("重载成功", cmds.has("mod_cmd", "iso_plug"))
    pm.unload_module("gm_plugin_iso_plug")
    check("重载后再卸载零残留",
          not cmds.has("mod_cmd") and not evs.has_handlers("engine_start"))
    pygame.quit()


def test_plugin_unload_residue():
    """卸载特效/过渡/调试插件后, 动作/效果/文字模式/覆盖层/快捷键零残留。"""
    print("== 特效插件卸载零残留 ==")
    e = make_engine()
    from framework.api.plugin import PluginManager
    import tempfile
    plug_dir = tempfile.mkdtemp()
    # 复制内置插件到临时目录, 模拟可装卸的插件
    import shutil
    for name in ("fx", "custom_actions", "transitions_plus", "debug_mode"):
        src = os.path.join(_ROOT, "framework", "plugins", name + ".py")
        shutil.copy(src, os.path.join(plug_dir, name + ".py"))
    pm = PluginManager(e)
    pm.discover(plug_dir, {"only": ["fx", "custom_actions",
                                    "transitions_plus", "debug_mode"]})
    d = e.display
    check("fx 指令注册", e.commands.has("shake", "fx"))
    check("custom_actions 动作注册", "explode" in e.actions)
    check("custom_actions 立绘效果注册", "wobble" in d.sprite_effects)
    check("custom_actions 文字模式注册", "wave" in d.text_modes)
    check("过渡注册", "wipe" in d.transitions)
    check("覆盖层注册", len(d._effect_overlays) >= 2)
    check("快捷键注册", e.keybinds.get_key("debug_toggle", "primary") is not None)
    # 逐个卸载, 断言零残留
    pm.unload_module("gm_plugin_debug_mode")
    check("卸载后快捷键清除", e.keybinds.get_key("debug_toggle", "primary") is None)
    check("卸载后设置项清除", "debug_toggle" not in e.settings.items)
    pm.unload_module("gm_plugin_transitions_plus")
    check("卸载后过渡清除", "wipe" not in d.transitions)
    pm.unload_module("gm_plugin_custom_actions")
    check("卸载后动作清除", "explode" not in e.actions
          and "do_action" not in e.commands.names("custom_actions"))
    check("卸载后立绘效果清除", "wobble" not in d.sprite_effects)
    check("卸载后文字模式清除", "wave" not in d.text_modes)
    pm.unload_module("gm_plugin_fx")
    check("卸载后 fx 指令清除", not e.commands.has("shake", "fx"))
    check("卸载后覆盖层清除", len(d._effect_overlays) == 0)
    check("main 域无污染", not e.commands.has("shake", "main"))
    pygame.quit()


def test_voice_setting_persistence():
    print("== 动态 voice 设置持久化 ==")
    e = make_engine()
    e.runtime.characters["producer"] = {"voice_volume": 0.5}
    e.settings.set("voice:producer", 0.3)
    data = e.save.get_settings() or {}
    check("动态 voice 项已保存", data.get("voice:producer") == 0.3)
    e.runtime.characters["producer"]["voice_volume"] = 1.0
    e.settings.load()
    check("动态 voice 项已恢复",
          abs(e.runtime.characters["producer"]["voice_volume"] - 0.3) < 1e-6)
    pygame.quit()


def main():
    test_ui_sound()
    test_evaluate()
    test_show_pose_with_effect()
    test_plugin_isolation()
    test_plugin_unload_residue()
    test_voice_setting_persistence()
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    os._exit(main())
