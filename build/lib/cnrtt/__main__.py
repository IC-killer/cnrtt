"""支持 `python -m cnrtt` 方式启动。

委托给 cnrtt.cli.main，支持 --headless / --with-agent / --port / --agent-token 参数。
"""

from cnrtt.cli import main

if __name__ == "__main__":
    main()
