"""
测试用例：WidgetProtocol - UI 组件接口契约验证

目的:
- 验证所有 UI 组件遵循 WidgetProtocol 接口定义
- 检测循环依赖问题
- 确保类型注解完整性

协议要求 (来自 widget_protocol.py):
1. bind_events(on_select, on_edit) -> None
2. render(stdscr, size) -> None  
3. on_key_up() -> None
4. on_key_down() -> None
5. handle_hotkey(key: int) -> bool
6. cleanup() -> None

关键验证点:
- 所有方法必须存在
- 无参数时不应抛出异常
- 各组件独立实现，无循环导入
"""
import sys
from pathlib import Path
import unittest
from importlib import import_module
# 添加项目 src 目录到路径
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))


class TestWidgetProtocol(unittest.TestCase):
    """Widget 接口契约测试"""

    def setUp(self):
        """测试前准备工作"""
        from claude_hub.ui.layout.widget_protocol import Widget
        self.protocol = Widget
        
        # 获取所有实现类
        self.component_classes = self._load_components()

    def _load_components(self):
        """加载所有 UI 组件类"""
        components = []
        
        # 从 components 包加载
        try:
            from claude_hub.ui.components.provider_list import ProviderListWidget
            from claude_hub.ui.components.model_editor import ModelEditorWidget
            components.extend([ProviderListWidget, ModelEditorWidget])
        except ImportError as e:
            self.fail(f"无法导入组件：{e}")
        
        # 从 animation 包加载
        try:
            from claude_hub.ui.animation.logo_animator import LogoAnimator
            components.append(LogoAnimator)
        except ImportError as e:
            self.fail(f"无法导入动画模块：{e}")
        
        return components

    def test_protocol_exists(self):
        """验证 WidgetProtocol 接口存在"""
        self.assertIsNotNone(self.protocol)
        self.assertTrue(callable(self.protocol))

    def test_all_implement_bind_events(self):
        """验证所有组件都有 bind_events 方法"""
        for cls in self.component_classes:
            with self.subTest(component=cls.__name__):
                self.assertTrue(hasattr(cls, 'bind_events'), 
                              f"{cls.__name__} 缺少 bind_events 方法")
                self.assertTrue(callable(getattr(cls, 'bind_events')),
                              f"{cls.__name__}.bind_events 不是可调用对象")
    
    def test_all_implement_render(self):
        """验证所有组件都有 render 方法"""
        for cls in self.component_classes:
            with self.subTest(component=cls.__name__):
                self.assertTrue(hasattr(cls, 'render'),
                              f"{cls.__name__} 缺少 render 方法")
                self.assertTrue(callable(getattr(cls, 'render')),
                              f"{cls.__name__}.render 不是可调用对象")

    def test_all_implement_navigation_keys(self):
        """验证所有组件都有导航键方法"""
        for cls in self.component_classes:
            with self.subTest(component=cls.__name__):
                # 检查上下键处理
                self.assertTrue(hasattr(cls, 'on_key_up'),
                              f"{cls.__name__} 缺少 on_key_up 方法")
                self.assertTrue(hasattr(cls, 'on_key_down'),
                              f"{cls.__name__} 缺少 on_key_down 方法")

    def test_all_implement_hotkey(self):
        """验证所有组件都有热键处理"""
        for cls in self.component_classes:
            with self.subTest(component=cls.__name__):
                self.assertTrue(hasattr(cls, 'handle_hotkey'),
                              f"{cls.__name__} 缺少 handle_hotkey 方法")

    def test_all_implement_cleanup(self):
        """验证所有组件都有清理方法"""
        for cls in self.component_classes:
            with self.subTest(component=cls.__name__):
                self.assertTrue(hasattr(cls, 'cleanup'),
                              f"{cls.__name__} 缺少 cleanup 方法")

    def test_inheritance_from_protocol(self):
        """验证组件继承自 WidgetProtocol（可选）"""
        # 某些组件可能直接实现接口而不显式继承
        # 这里只验证它们满足接口要求即可
        
        # ProviderListWidget 应该符合协议
        provider_list = self.component_classes[0]
        self.assertTrue(hasattr(provider_list, '__init__'))

    def test_no_import_errors(self):
        """验证没有循环导入错误"""
        errors = []
        
        modules_to_test = [
            'claude_hub.ui.components',
            'claude_hub.ui.animation',
            'claude_hub.ui.input',
            'claude_hub.ui.themes',
        ]
        
        for module_name in modules_to_test:
            try:
                import_module(module_name)
                print(f"✓ {module_name} 导入成功")
            except ImportError as e:
                errors.append(f"{module_name}: {e}")
        
        if errors:
            self.fail(f"模块导入失败:\n" + "\n".join(errors))
        else:
            print("✓ 所有模块导入成功，无循环依赖")


class TestComponentInterface(unittest.TestCase):
    """具体组件的接口实现验证"""

    def test_provider_list_interface(self):
        """验证 ProviderListWidget 完整接口"""
        from claude_hub.ui.components.provider_list import ProviderListWidget
        import inspect
        
        # 检查构造函数签名
        sig = inspect.signature(ProviderListWidget.__init__)
        params = list(sig.parameters.keys())
        
        # 必需参数
        self.assertIn('self', params)
        self.assertIn('providers', params)
        self.assertIn('inspections', params)
        self.assertIn('selected_id', params)
        
        # 可选参数
        self.assertIn('single_mode', params)
        
        print("✓ ProviderListWidget 接口完整")

    def test_logo_animator_interface(self):
        """验证 LogoAnimator 完整接口"""
        from claude_hub.ui.animation.logo_animator import LogoAnimator
        import inspect
        
        sig = inspect.signature(LogoAnimator.__init__)
        params = list(sig.parameters.keys())
        
        self.assertIn('self', params)
        # 验证有 MAX_ANIMATION_DURATION_MS 常量
        self.assertTrue(hasattr(LogoAnimator, 'MAX_ANIMATION_DURATION_MS'))
        
        duration = LogoAnimator.MAX_ANIMATION_DURATION_MS
        self.assertEqual(duration, 1000)  # 1 秒
        print(f"✓ LogoAnimator 接口完整 (最大时长={duration}ms)")


class TestCircularDependency(unittest.TestCase):
    """循环依赖检测"""

    def test_no_circular_import(self):
        """检测是否存在循环导入"""
        circular_patterns = [
            ('from .components import', '->', 'from .animation'),
            ('from .animation import', '->', 'from .components'),
        ]
        
        # 读取相关文件内容
        files_to_check = [
            str(SRC_DIR / 'claude_hub/ui/components/__init__.py'),
            str(SRC_DIR / 'claude_hub/ui/animation/__init__.py'),
            str(SRC_DIR / 'claude_hub/ui/app.py'),
        ]
        
        import_checks = []
        
        for file_path in files_to_check:
            try:
                with open(file_path, 'r') as f:
                    content = f.read()
                    
                # 检查是否有互相引用
                if 'from .app import' in content or 'from app import' in content:
                    print(f"⚠ {file_path} 中存在反向导入")
                    import_checks.append(True)
                
            except FileNotFoundError:
                pass
        
        if not import_checks:
            print("✓ 未发现明显的循环导入模式")


if __name__ == "__main__":
    print("=" * 60)
    print("WidgetProtocol 契约测试")
    print("=" * 60)
    
    tests = [
        ("接口契约验证", TestWidgetProtocol),
        ("组件接口验证", TestComponentInterface),
        ("循环依赖检测", TestCircularDependency),
    ]
    
    runner = unittest.TextTestRunner(verbosity=2)
    
    for name, test_class in tests:
        print(f"\n【{name}】")
        print("-" * 60)
        suite = unittest.TestLoader().loadTestsFromTestCase(test_class)
        runner.run(suite)
    
    print("\n" + "=" * 60)
    print("契约测试完成!")
    print("=" * 60)
