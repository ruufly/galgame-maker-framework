"""插件系统: 基类、装饰器与加载管理器。

一个插件就是一个 Python 模块 (或类), 放在 ``framework/plugins/``
目录下会被引擎自动发现加载。目录下以下划线开头的文件会被忽略。

推荐写法 (模块级函数 + 装饰器, 最简单)::

    # framework/plugins/my_plugin.py
    from framework.api import event_listener, command

    @event_listener("engine_start")
    def on_start(engine, **kw):
        print("插件加载完成!")

    @command("greet")
    def greet(engine, stmt, **kw):
        engine.say("插件", "这是一条由插件指令生成的对话")

也可以写类形式 (需要生命周期管理时)::

    class MyPlugin(Plugin):
        name = "my_plugin"
        version = "1.0"

        def on_load(self):
            # 订阅事件 / 注册指令
            pass

        def on_unload(self):
            pass
"""

import importlib.util
import inspect
import os
import sys
from typing import List, Optional

# 装饰器: 给函数打标记, 加载时由 PluginManager 统一注册
_EVENT_ATTR = "_gm_event"
_COMMAND_ATTR = "_gm_command"


def event_listener(event_name: str):
    """装饰器: 把函数标记为某事件的处理器。"""
    def deco(fn):
        setattr(fn, _EVENT_ATTR, event_name)
        return fn
    return deco


def command(name: str):
    """装饰器: 把函数标记为一条自定义 DSL 指令。"""
    def deco(fn):
        setattr(fn, _COMMAND_ATTR, name)
        return fn
    return deco


class Plugin:
    """插件基类 (可选继承)。"""

    name: str = "unnamed_plugin"
    version: str = "0.1"

    def __init__(self, engine) -> None:
        self.engine = engine
        self._event_handlers = []
        self._command_handlers = []

    # ---- 生命周期钩子 -------------------------------------------------
    def on_load(self) -> None:
        """引擎加载插件时调用 (此时事件/指令尚未注册)。"""
        pass

    def on_unload(self) -> None:
        """引擎卸载插件时调用。"""
        pass

    # ---- 注册辅助 ----------------------------------------------------
    def listen(self, event_name: str):
        """实例方法版事件订阅, 返回装饰器。"""
        def deco(fn):
            self.engine.events.on(event_name, fn)
            self._event_handlers.append((event_name, fn))
            return fn
        return deco

    def add_command(self, name: str):
        """实例方法版指令注册 (命名空间 = 插件名), 返回装饰器。"""
        ns = getattr(self, "name", None) or "main"
        def deco(fn):
            self.engine.commands.register(name, fn, ns=ns)
            self._command_handlers.append((name, fn))
            return fn
        return deco

    def _cleanup(self) -> None:
        ns = getattr(self, "name", None) or "main"
        for name, fn in list(self._command_handlers):
            try:
                self.engine.commands.unregister(name, ns=ns)
            except Exception:
                pass
        self._command_handlers.clear()
        for ev, fn in list(self._event_handlers):
            # 取消订阅 (按保存的 (事件名, 函数) 精确移除, 不遍历全表)
            try:
                self.engine.events.off(ev, fn)
            except Exception:
                pass
        self._event_handlers.clear()


class PluginManager:
    """扫描并加载插件目录中的模块。"""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.plugins: List[Plugin] = []
        self.directory = None   # 最近一次 discover 的插件目录 (运行时装载用)
        self._modules = {}   # name -> module
        self._classes = {}   # name -> Plugin 类
        self._mod_regs = {}  # 模块级装饰器注册追踪: name -> {commands, events}
        self._inst_regs = {}  # 实例级装饰器注册追踪: id(inst) -> {commands, events}

    # ------------------------------------------------------------------
    def discover(self, directory: str, config: dict = None) -> List[str]:
        """扫描目录下所有 ``*.py`` (排除下划线开头) 并加载, 返回模块名列表。

        config: 可选插件装载配置
            {"only": [插件文件名...]}   只装载列出的
            {"except": [插件文件名...]} 排除列出的
        """
        config = config or {}
        only = config.get("only") or []
        except_ = config.get("except") or []
        self.directory = directory
        loaded = []
        if not os.path.isdir(directory):
            return loaded
        # 插件语言: <插件目录>/lang/<code>.json (ns="plugin")
        self.engine.i18n.load_dir(os.path.join(directory, "lang"),
                                  ns="plugin")
        for entry in sorted(os.listdir(directory)):
            if entry.startswith("_") or not entry.endswith(".py"):
                continue
            base = os.path.splitext(entry)[0]
            if only and base not in only:
                continue
            if base in except_:
                continue
            path = os.path.join(directory, entry)
            mod_name = "gm_plugin_" + base
            if self.load_module_from_path(mod_name, path):
                loaded.append(mod_name)
        return loaded

    # ------------------------------------------------------------------
    def load_module_from_path(self, mod_name: str, path: str) -> bool:
        """从文件路径加载一个插件模块。

        支持文件编解码钩子 "plugin" scope: 解密后的内容经临时文件交给
        importlib 加载 (加载完成后清理), 无钩子时原样加载。
        """
        import tempfile
        tmp_path = None
        try:
            load_path = path
            with open(path, "rb") as f:
                raw = f.read()
            decoded = self.engine._codec_decode("plugin", raw)
            if decoded != raw:
                tf = tempfile.NamedTemporaryFile(
                    suffix=".py", mode="wb", delete=False)
                tf.write(decoded)
                tf.close()
                tmp_path = tf.name
                load_path = tmp_path
            spec = importlib.util.spec_from_file_location(mod_name, load_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            self._modules[mod_name] = module
            self._mod_regs.setdefault(mod_name, {"commands": [], "events": []})
            self._register_from_module(module, mod_name)
            return True
        except Exception as exc:
            from framework.engine import log
            log.w("log.plugin.load_failed", path=path, exc=exc)
            return False
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def load(self, module) -> Optional[Plugin]:
        """加载一个已导入的模块 (或其内的 Plugin 子类)。"""
        if inspect.ismodule(module):
            mod_name = getattr(module, "__name__", None)
            self._register_from_module(module, mod_name)
            return None
        return self._instantiate(module)

    # ------------------------------------------------------------------
    def _register_from_module(self, module, mod_name: str = None) -> None:
        """扫描模块: 收集带装饰器标记的函数与 Plugin 子类。"""
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if inspect.isclass(obj) and issubclass(obj, Plugin) and obj is not Plugin:
                self._instantiate(obj, mod_name)
                continue
            if inspect.isfunction(obj):
                self._register_function(obj, mod_name)

    def _register_function(self, fn, mod_name: str = None,
                           inst: Plugin = None) -> None:
        """注册装饰器标记的函数 (事件/指令)。

        inst 非空时是类形式插件的实例方法: 命名空间取所属插件名,
        注册追踪挂在实例上 (卸载时精确清理, 不污染 main:: 域)。
        """
        ev = getattr(fn, _EVENT_ATTR, None)
        cmd = getattr(fn, _COMMAND_ATTR, None)
        if ev is not None:
            self.engine.events.on(ev, fn)
            if inst is not None:
                self._inst_regs.setdefault(id(inst), {"commands": [],
                                                      "events": []})
                self._inst_regs[id(inst)]["events"].append((ev, fn))
            elif mod_name:
                self._mod_regs.setdefault(mod_name, {"commands": [],
                                                     "events": []})
                self._mod_regs[mod_name]["events"].append((ev, fn))
        if cmd is not None:
            # 插件命名空间 = 插件文件名 (gm_plugin_shake -> shake);
            # 类形式插件: 所属模块名, 无模块时用类名/插件名
            if inst is not None:
                ns = (mod_name or getattr(inst, "name", None)
                      or type(inst).__name__).replace("gm_plugin_", "")
            else:
                ns = (mod_name or "main").replace("gm_plugin_", "")
            self.engine.commands.register(cmd, fn, ns=ns)
            if inst is not None:
                self._inst_regs.setdefault(id(inst), {"commands": [],
                                                      "events": []})
                self._inst_regs[id(inst)]["commands"].append((cmd, ns))
            elif mod_name:
                self._mod_regs.setdefault(mod_name, {"commands": [],
                                                     "events": []})
                self._mod_regs[mod_name]["commands"].append(cmd)

    def _instantiate(self, cls, mod_name: str = None) -> Optional[Plugin]:
        try:
            inst = cls(self.engine)
        except Exception as exc:
            from framework.engine import log
            log.w("log.plugin.instantiate_failed", cls=cls, exc=exc)
            return None
        if not isinstance(inst, Plugin):
            return None
        inst.on_load()
        # 重新扫描实例方法上的装饰器标记 (类形式插件也支持装饰器;
        # 命名空间 = 所属插件名, 注册进实例级追踪以便卸载)
        for name in dir(inst):
            obj = getattr(inst, name)
            if callable(obj) and not name.startswith("__"):
                self._register_function(obj, mod_name=mod_name, inst=inst)
        self.plugins.append(inst)
        self._classes[inst.name] = inst
        from framework.engine import log
        log.i("log.plugin.loaded", name=inst.name, version=inst.version)
        return inst

    # ------------------------------------------------------------------
    def unload(self, plugin) -> None:
        try:
            plugin.on_unload()
        except Exception as exc:
            from framework.engine import log
            log.w("log.plugin.unload_failed", name=plugin.name, exc=exc)
        plugin._cleanup()
        # 清理实例级装饰器注册 (类形式插件的 @command/@event_listener)
        regs = self._inst_regs.pop(id(plugin), None)
        if regs:
            for cmd_name, ns in regs.get("commands", []):
                self.engine.commands.unregister(cmd_name, ns=ns)
            for ev, fn in regs.get("events", []):
                self.engine.events.off(ev, fn)
        if plugin in self.plugins:
            self.plugins.remove(plugin)
        self._classes.pop(plugin.name, None)

    def unload_module(self, mod_name: str) -> None:
        """卸载整个插件模块: 类实例 + 模块级指令/事件/订阅。"""
        # 先卸载该模块定义的 Plugin 类实例
        for inst in list(self.plugins):
            try:
                mod = inspect.getmodule(type(inst))
                if mod is not None and getattr(mod, "__name__", "") == mod_name:
                    self.unload(inst)
            except Exception:
                pass
        # 再清模块级注册 (指令/事件/模块表)
        regs = self._mod_regs.pop(mod_name, None)
        if regs:
            ns = mod_name.replace("gm_plugin_", "")
            for cmd_name in regs.get("commands", []):
                self.engine.commands.unregister(cmd_name, ns=ns)
            for ev, fn in regs.get("events", []):
                self.engine.events.off(ev, fn)
        self._modules.pop(mod_name, None)
        # 清理 sys.modules 残留, 避免重载同名插件时读到旧模块状态
        import sys as _sys
        _sys.modules.pop(mod_name, None)

    def unload_all(self) -> None:
        for plugin in list(self.plugins):
            self.unload(plugin)
        # 清理所有模块级注册
        for mod_name in list(self._mod_regs.keys()):
            self.unload_module(mod_name)
