# A 股 / 港股报价管道 → Portfolio Performance

给 Portfolio Performance 喂 A 股和港股历史报价。GitHub Actions 每个交易日自动抓取，
生成静态 JSON 发布到 GitHub Pages，PP 用「JSON」数据源直接拉取。

配置一次，之后零维护。不需要 API Key，不受本地网络和 IP 限流影响。

---

## 文件放哪

```
你的仓库/
├── fetch.py
├── securities.csv
├── docs/
│   └── json/              ← 脚本自动生成，不用手工建
└── .github/
    └── workflows/
        └── update-quotes.yml
```

注意 `update-quotes.yml` 必须放在 `.github/workflows/` 目录下，路径错了 Actions 不会运行。

---

## 第一步：本地先验证（重要）

**不要跳过这一步。** 先确认数据源通、字段映射对，再交给 GitHub。

在放着 `fetch.py` 的目录下执行：

```bash
python fetch.py --test SH600004
```

正常会输出条数、日期区间和最后一条记录。**把最后一条的 close 和你行情软件里的收盘价对一下**，
一致才说明字段映射正确。

再测一个港股和一个深市的：

```bash
python fetch.py --test HK00700
python fetch.py --test SZ000001
```

三个都通，就可以往下走。如果东财失败会自动切腾讯，报错信息里会写明两家各自的失败原因。

---

## 第二步：建仓库并开 Pages

1. 在 GitHub 新建一个仓库（**建议设为 Public**，Private 仓库的 Pages 需要付费方案）
2. 把上面四个文件按目录结构推上去
3. 仓库 Settings → Pages → Source 选 **Deploy from a branch**，
   分支选 `main`，目录选 **`/docs`**，保存
4. Actions 页面点 `update-quotes` → **Run workflow** 手动跑一次

跑完后仓库里会多出 `docs/json/SH600004.json` 这类文件。

你的 Feed URL 就是：

```
https://<你的用户名>.github.io/<仓库名>/json/SH600004.json
```

先用浏览器打开确认能看到 JSON 内容，再去配 PP。

`docs/json/_index.json` 会列出每个标的的更新状态和失败原因，排查问题看这个。

---

## 第三步：配置 Portfolio Performance

对每个 A 股 / 港股标的：

1. 「添加投资品」→ **空白投资品**
2. 证券主要信息：名称、**币种选 CNY（港股选 HKD）**，代码随意填
3. 切到「**历史报价**」标签页，提供方选 **JSON**
4. 填三个字段：

   | 字段 | 填什么 |
   |---|---|
   | Feed URL | `https://<用户名>.github.io/<仓库名>/json/SH600004.json` |
   | Date（日期） | `$[*].date` |
   | Close（收盘价） | `$[*].close` |

5. 想要更完整可以再填 `$[*].high`、`$[*].low`、`$[*].volume`
6. 点「显示服务器响应」确认拿得到数据，然后确定

以后新增标的：往 `securities.csv` 加一行 → Actions 自动生成 JSON → PP 里建标的填 URL。

---

## 为什么默认「不复权」

`ADJUST=0`，这是刻意的，**不建议改**。

PP 的算法是：用**实际成交价**记录你的买卖，股息单独作为一笔交易录入，然后自己算总回报。
如果喂前复权价格，历史价格会在每次分红时整体平移，你两年前的买入价就对不上当时的行情了，
成本和收益率全部失真。

不复权价格在除权日会有跳空缺口，这是正常的——那部分收益由你录入的股息交易来体现，
不需要价格序列去补。

---

## 常见问题

**Actions 报错 403 / 无法 push**
仓库 Settings → Actions → General → Workflow permissions
选「Read and write permissions」。

**某个标的一直 FAIL**
看 `_index.json` 里的 error。多半是 market 写错（沪市 SH、深市 SZ、北交所 BJ、港股 HK），
或者港股代码没补零（腾讯要写 `00700` 不是 `700`）。

**GitHub Actions 的 IP 被源站拒绝**
理论上有可能。如果本地测试通过但 Actions 里全部失败，就是这个原因。
备选是改用自己电脑上的定时任务（Windows 任务计划程序每天跑一次 `fetch.py`），
输出的 JSON 放到任意能公网访问的位置，或者干脆让 PP 读本地文件路径。

**新股 / 长期停牌**
脚本会跳过空数据并记录到 `_index.json`，不影响其他标的。

**想加指数做基准**
上证指数 `SH000001`、沪深300 `SH000300`、创业板指 `SZ399006`，
写进 `securities.csv` 一样能抓，PP 里可以当基准用。
