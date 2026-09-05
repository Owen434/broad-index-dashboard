import pandas as pd
import akshare as ak
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import webbrowser
import os

# ==========================================
# 1. 获取并清洗数据
# ==========================================
print("正在获取 COMEX 黄金数据...")
gold_comex_df = ak.futures_foreign_hist(symbol="GC")
gold_comex_df['date'] = pd.to_datetime(gold_comex_df['date'])
gold_comex_df = gold_comex_df.sort_values('date')

# ==========================================
# 2. 定义绘图函数（无量化面板，仅图例联动 + 分位线）
# ==========================================
def plot_ma_and_slopes_html(df, windows=[5, 20, 30, 60], num_days=252, output_filename="gold_ma_slopes_interactive.html"):
    """
    使用 Plotly 计算均线及斜率，生成可交互 HTML 图表
    - 暗色主题
    - 图例合并：点击均线，同周期的均线、斜率、分位线同步显示/隐藏
    - 斜率分位线（25% 和 75%）默认隐藏，与同组联动
    """
    # 拷贝数据，确保有足够的历史数据计算早期均线
    data = df.tail(num_days + max(windows)).copy()
    
    # 计算均线和每日斜率（当日值 - 前一日值）
    for w in windows:
        data[f'MA_{w}'] = data['close'].rolling(w).mean()
        data[f'Slope_{w}'] = data[f'MA_{w}'].diff()
        
    # 截取最终需要展示的最近交易日
    plot_data = data.tail(num_days).copy()
    
    # 创建双子图，共享 X 轴
    fig = make_subplots(
        rows=2, cols=1, 
        shared_xaxes=True, 
        vertical_spacing=0.08,
        subplot_titles=(f"COMEX黄金价格与均线走势 (最近 {num_days} 个交易日)", "均线每日斜率 (大于0看多，小于0看空)")
    )
    
    # 颜色方案（适配暗色主题，高饱和度）
    colors = {
        5: '#2ca02c',   # 绿色
        20: '#ff7f0e',  # 橙色
        30: '#1f77b4',  # 蓝色
        60: '#9467bd'   # 紫色
    }
    
    # ---- 收盘价（独立图例） ----
    fig.add_trace(
        go.Scatter(x=plot_data['date'], y=plot_data['close'], name='收盘价', line=dict(color='#ffd700', width=1.8)),
        row=1, col=1
    )
    
    # ---- 每个周期的均线、斜率、分位线（共享图例组） ----
    for w in windows:
        group_id = f"group_{w}"
        current_color = colors.get(w)
        
        # ① 均线（显示图例，作为组代表）
        fig.add_trace(
            go.Scatter(
                x=plot_data['date'], y=plot_data[f'MA_{w}'],
                name=f'{w}日均线',
                line=dict(color=current_color, width=1.5),
                legendgroup=group_id,
                showlegend=True
            ),
            row=1, col=1
        )
        
        # ② 斜率（隐藏图例，但与均线同组）
        fig.add_trace(
            go.Scatter(
                x=plot_data['date'], y=plot_data[f'Slope_{w}'],
                name=f'{w}日斜率',
                line=dict(color=current_color, width=1.8),
                legendgroup=group_id,
                showlegend=False
            ),
            row=2, col=1
        )
        
        # ③ 分位数线（25% 和 75%，初始隐藏，同组联动）
        slope_series = plot_data[f'Slope_{w}']
        q5 = slope_series.quantile(0.05)
        q95 = slope_series.quantile(0.95)
        
        fig.add_trace(
            go.Scatter(
                x=[plot_data['date'].min(), plot_data['date'].max()],
                y=[q95, q95],
                name=f'{w}日斜率 [95%分位数]',
                mode='lines',
                line=dict(color=current_color, width=1, dash="dot"),
                legendgroup=group_id,
                visible='legendonly',   # 默认隐藏
                showlegend=False
            ),
            row=2, col=1
        )
        fig.add_trace(
            go.Scatter(
                x=[plot_data['date'].min(), plot_data['date'].max()],
                y=[q5, q5],
                name=f'{w}日斜率 [5%分位数]',
                mode='lines',
                line=dict(color=current_color, width=1, dash="dot"),
                legendgroup=group_id,
                visible='legendonly',
                showlegend=False
            ),
            row=2, col=1
        )
        
    # ---- 零线基准 ----
    fig.add_shape(
        type="line", x0=plot_data['date'].min(), y0=0, x1=plot_data['date'].max(), y1=0,
        line=dict(color="Gray", width=1.5, dash="dash"),
        row=2, col=1
    )

    # ---- 暗色主题布局 ----
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#111111",
        plot_bgcolor="#1e1e1e",
        height=850,
        title_text=f"黄金多周期均线及动能(斜率)分析系统",
        title_x=0.5,
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.02,
            xanchor="right", x=1,
            groupclick="togglegroup"   # 点击组内任意项，整组切换可见性
        )
    )
    
    fig.update_yaxes(title_text="价格 (美元)", gridcolor="#333333", row=1, col=1)
    fig.update_yaxes(title_text="斜率值", gridcolor="#333333", row=2, col=1)
    # 横轴统一用 2026-03-06 这种格式，不用 Plotly 默认的英文月份缩写
    fig.update_xaxes(tickformat="%Y-%m-%d", hoverformat="%Y-%m-%d",
                      gridcolor="#333333", row=1, col=1)
    fig.update_xaxes(title_text="日期", tickformat="%Y-%m-%d", hoverformat="%Y-%m-%d",
                      gridcolor="#333333", row=2, col=1)
    
    # 保存为 HTML 文件（config 加 responsive，让图表随窗口/手机屏幕宽度自适应）
    fig.write_html(output_filename, config={'responsive': True})
    # Plotly 默认不写 viewport meta，手机打开会按桌面宽度渲染再整体缩小；补一行就好
    with open(output_filename, 'r', encoding='utf-8') as f:
        html = f.read()
    if 'name="viewport"' not in html:
        html = html.replace(
            '<head>',
            '<head>\n    <meta name="viewport" content="width=device-width, initial-scale=1">',
            1
        )
        with open(output_filename, 'w', encoding='utf-8') as f:
            f.write(html)
    print(f"交互式图表已成功生成: {os.path.abspath(output_filename)}")
    
    # 自动在浏览器中打开（若需可取消注释）
    # webbrowser.open('file://' + os.path.abspath(output_filename))

# ==========================================
# 3. 调用函数生成交互图表
# ==========================================
if __name__ == "__main__":
    # 参数可根据需要修改
    plot_ma_and_slopes_html(
        gold_comex_df, 
        windows=[5, 20, 30, 60],   # 均线周期
        num_days=252,              # 展示最近252个交易日
        output_filename="gold_ma_slopes_interactive.html"
    )