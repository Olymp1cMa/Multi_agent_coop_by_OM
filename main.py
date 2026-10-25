"""程序入口：只选择成员数量，然后启动协作。"""

from collaboration import run_collaboration

# 选择任意正整数数量的智能体，把数量交给协作运行模块。
def main():
    while True:
        choice = input("开启几个智能体（含协调者）？请输入正整数（exit 退出）：").strip().lower()
        if choice == "exit":
            return
        if choice.isdecimal():
            count = int(choice)
            if count >= 1:
                run_collaboration(count)
                return
        print("请输入正整数，或 exit。")


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print("\n已退出。")
    except Exception as error:
        print(f"\n程序出错：{error}\n本次协作已结束，不会自动重试；已产生的记录保留在 runs 中。")
        raise SystemExit(1)
