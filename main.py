from manus_client import ManusClient

def main():
    print("🤖 Manus GPT-4o Proxy is live.")
    prompt = input("🧠 Enter your prompt: ")

    agent = ManusClient()
    answer = agent.ask_manus(prompt)

    print("\n📩 Response from Manus.AI:")
    print(answer)

if __name__ == "__main__":
    main()
