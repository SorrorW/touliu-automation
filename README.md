# 投流自动化工具集

Windows 桌面投流工具与企业微信表格轮询。仓库目录与当前部署保持一致：

```text
投流/                      # 千川、视频号、桌面 UI 与浏览器互斥
wecom-agent/               # 智能表格、日报和计划任务入口
docs/main-account-migration/ # 四主品主号流程、恢复与验证
```

## 当前主号流程

读取主表和旧素材表，按商品 ID 匹配四个主品。先完整核实对应计划的有效绑定；未绑定或已删除的达人通过主号合作达人授权，两项权限生效后加入既有主品计划，并重新读取平台、落库及回写表格。

已有有效绑定直接闭环；等待确认保存断点并释放浏览器；跨表同达人同商品只执行一次。明确人工停投、佣金限制、账号及商品校验继续生效。已有分部计划的 ROI、预算、启停和日报由原流程负责。

完整规则见 [主号流程说明](docs/main-account-migration/README.md)。

## 环境与启动

Windows、Microsoft Edge、Python 3.12+。安装根目录 requirements.txt 的依赖，再从 `投流` 目录运行：

```powershell
python -m webui.desktop
```

便携部署仍使用 `投流/runtime/pythonw.exe` 和 `投流/启动UI.bat`，便携 Python、登录资料、数据库与配置不随源码提交。

复制 `wecom-agent/config.json.example` 为本机的 `config.json` 并填写凭据。浏览器使用专用 CDP 9223；首次需要操作人员登录授权平台。自动轮询由桌面托管，关闭桌面会停止后续轮询。

## 验证

```powershell
python wecom-agent/test_main_account_flow.py
cd 投流
python -m unittest 巨量千川.test_auth_policy
```

真实平台仍需确认账号、商品、目标计划及达人授权；测试通过不表示业务记录全部加入成功。

## 源码同步说明

本次在原初始版本提交之上，对齐部署中的 `投流` 和 `wecom-agent` 两个同级目录，并纳入当前生产依赖。运行数据库、合作码、凭据、群机器人地址、人工名单、日志和逐条业务核验结果保留在本机。

## License

[MIT](LICENSE)
