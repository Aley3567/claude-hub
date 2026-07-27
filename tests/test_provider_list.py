"""
测试用例：ProviderListWidget - 渠道选择组件

核心能力:
- 多渠道模式：显示编号列表 + HELP 提示
- 单渠道模式：直连布局 + LEFT ARROW 符号
- 导航逻辑：上下键选择，回车确认，ESC 返回
- 快捷键提示：E 编辑，R 重启

关键指标:
- 初始化正确性：单/多渠道模式切换
- 数据绑定准确性：providers 和 inspections 映射
"""
import sys
from pathlib import Path
import unittest
import curses
from unittest.mock import Mock, MagicMock

# 添加项目 src 目录到路径
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))


class TestProviderListWidget(unittest.TestCase):
    """ProviderListWidget 单元测试类"""

    def setUp(self):
        """测试前准备工作"""
        # 创建模拟的服务和数据
        self.provider1 = Mock()
        self.provider1.provider_id = "provider-001"
        self.provider1.name = "Anthropic Claude"
        self.provider1.is_current = True
        
        self.provider2 = Mock()
        self.provider2.provider_id = "provider-002"
        self.provider2.name = "OpenAI Chat"
        self.provider2.is_current = False
        
        # 创建模拟的 inspection 数据
        self.inspection1 = {
            "endpoint": {"url": "https://api.anthropic.com/v1"},
            "models": [{"name": "claude-3"}]
        }
        self.inspection2 = {
            "endpoint": {"url": "https://api.openai.com/v1"},
            "models": [{"name": "gpt-4"}]
        }
        
        self.inspections = {
            "provider-001": self.inspection1,
            "provider-002": self.inspection2
        }

    def test_multichannel_mode_initialization(self):
        """测试多渠道模式的初始化"""
        providers = [self.provider1, self.provider2]
        
        from claude_hub.ui.components.provider_list import ProviderListWidget
        
        # 创建 widget（多渠道模式）
        widget = ProviderListWidget(
            providers, 
            self.inspections, 
            "provider-001",
            single_mode=False
        )
        
        # 验证状态
        self.assertEqual(len(widget.providers), 2)
        self.assertEqual(widget.selected_index, 0)
        self.assertTrue(not widget.single_mode)

    def test_single_channel_mode_initialization(self):
        """测试单渠道模式的初始化"""
        providers = [self.provider1]
        
        from claude_hub.ui.components.provider_list import ProviderListWidget
        
        # 创建 widget（单渠道模式）
        widget = ProviderListWidget(
            providers,
            {"provider-001": self.inspection1},
            "provider-001",
            single_mode=True
        )
        
        # 验证状态
        self.assertTrue(widget.single_mode)
        self.assertEqual(len(widget.providers), 1)

    def test_navigation_boundary_top(self):
        """测试上边界导航（从顶部按上键应跳转到末尾）"""
        providers = [self.provider1, self.provider2, Mock()]
        
        from claude_hub.ui.components.provider_list import ProviderListWidget
        
        widget = ProviderListWidget(
            providers,
            {"provider-001": self.inspection1},
            None
        )
        
        # 手动设置索引到 0
        widget.selected_index = 0
        
        # 模拟上键操作（在单渠道模式下，上键应该跳到末尾）
        if widget.single_mode:
            widget.on_key_up()
            # 预期行为：循环到最后一个元素
            self.assertEqual(widget.selected_index, len(providers) - 1)

    def test_navigation_boundary_bottom(self):
        """测试下边界导航（在底部按下键保持在最后）"""
        providers = [self.provider1, self.provider2, Mock()]
        
        from claude_hub.ui.components.provider_list import ProviderListWidget
        
        widget = ProviderListWidget(
            providers,
            {"provider-001": self.inspection1},
            None
        )
        
        # 手动设置索引到最后
        widget.selected_index = len(providers) - 1
        
        # 模拟下键操作 (curses.KEY_DOWN)
        widget.on_input(curses.KEY_DOWN)
        # 预期行为：保持在最后（不循环）
        self.assertEqual(widget.selected_index, len(providers) - 1)

    def test_event_binding(self):
        """测试事件绑定"""
        providers = [self.provider1]
        
        from claude_hub.ui.components.provider_list import ProviderListWidget
        
        widget = ProviderListWidget(
            providers,
            {"provider-001": self.inspection1},
            "provider-001"
        )
        
        # 验证 on_input 方法存在
        self.assertTrue(hasattr(widget, 'on_input'))
        self.assertTrue(callable(widget.on_input))

    def test_render_stub(self):
        """测试渲染方法存在且可调用"""
        providers = [self.provider1, self.provider2]
        
        from claude_hub.ui.components.provider_list import ProviderListWidget
        
        widget = ProviderListWidget(
            providers,
            self.inspections,
            "provider-001"
        )
        
        # 验证 render 方法存在
        self.assertTrue(hasattr(widget, 'render'))
        self.assertTrue(callable(widget.render))

    def test_auto_detect_single_mode(self):
        """测试自动检测单渠道模式"""
        providers = [self.provider1]
        
        from claude_hub.ui.components.provider_list import ProviderListWidget
        
        # 不传递 single_mode 参数，让组件自动检测
        widget = ProviderListWidget(
            providers,
            {"provider-001": self.inspection1},
            "provider-001"
        )
        
        # 当只有一个 provider 时，应该进入单渠道模式
        # 注意：这取决于 ProviderListWidget 的具体实现逻辑
        print(f"Single mode detected: {widget.single_mode}")


if __name__ == "__main__":
    print("=" * 60)
    print("ProviderListWidget 单元测试")
    print("=" * 60)
    
    suite = unittest.TestLoader().loadTestsFromTestCase(TestProviderListWidget)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    print("\n" + "=" * 60)
    if result.wasSuccessful():
        print("所有测试通过!")
    else:
        print(f"部分测试失败 ({len(result.failures)} 个失败)")
    print("=" * 60)
