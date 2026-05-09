import asyncio
import json
import os
import random

import websockets

# Идентификатор задаётся из docker-compose, поэтому один и тот же код
# можно запускать сразу в пяти контейнерах: agent_1 ... agent_5.
AGENT_ID = os.getenv("AGENT_ID", "agent_1")
SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://localhost:8000/ws/agent")

# Параметры эмуляции вынесены в окружение, чтобы на защите можно было
# ускорить или замедлить покупку билета без переписывания кода.
SUCCESS_PROBABILITY = float(os.getenv("SUCCESS_PROBABILITY", "0.7"))
BUY_DELAY_MIN = float(os.getenv("BUY_DELAY_MIN", "0.5"))
BUY_DELAY_MAX = float(os.getenv("BUY_DELAY_MAX", "200"))

current_job: asyncio.Task | None = None
current_task_id: int | None = None


async def try_buy_ticket(route_id: int, date: str, passenger: dict) -> dict:
    """Эмуляция запроса к внешнему билетному серверу.

    Агент ждёт случайное время, а затем с вероятностью 70% считает,
    что билет найден. Формат ответа оставлен таким же, как в примере.
    """

    await asyncio.sleep(random.uniform(BUY_DELAY_MIN, BUY_DELAY_MAX))

    if random.random() < SUCCESS_PROBABILITY:
        return {
            "success": True,
            "ticket": {
                "number": f"T{random.randint(10000000, 99999999)}",
                "date": date,
                "time": f"{random.randint(6, 22)}:{random.randint(0, 59):02d}",
            },
        }

    return {
        "success": False,
        "reason": "no_tickets",
    }


async def process_task(task_data: dict, ws) -> None:
    """Обработка одной заявки.

    Даты перебираются по порядку. Если билет найден хотя бы на одну дату,
    агент сразу отправляет успешный результат. Если дат больше нет — отправляет
    отказ. При abort задача отменяется и результат на сервер не уходит.
    """

    global current_task_id

    task_id = task_data["task_id"]
    route_id = task_data["route_id"]
    dates = task_data["dates"]
    passenger = task_data["passenger"]

    current_task_id = task_id

    print(f"[{AGENT_ID}] Task {task_id}: route_id={route_id}, dates={dates}")

    try:
        for date in dates:
            print(f"[{AGENT_ID}] Trying date {date}")
            result = await try_buy_ticket(route_id, date, passenger)

            if result["success"]:
                await ws.send(
                    json.dumps(
                        {
                            "type": "task_result",
                            "task_id": task_id,
                            "success": True,
                            "ticket": result["ticket"],
                        },
                        ensure_ascii=False,
                    )
                )
                print(f"[{AGENT_ID}] Task {task_id} success! Ticket: {result['ticket']['number']}")
                return

        await ws.send(
            json.dumps(
                {
                    "type": "task_result",
                    "task_id": task_id,
                    "success": False,
                    "reason": "no_tickets_for_any_date",
                },
                ensure_ascii=False,
            )
        )
        print(f"[{AGENT_ID}] Task {task_id} failed")

    except asyncio.CancelledError:
        # Это отличие от простого учебного примера: abort действительно
        # останавливает текущую покупку и не даёт отправить устаревший результат.
        print(f"[{AGENT_ID}] Task {task_id} aborted")

    finally:
        current_task_id = None


async def heartbeat(ws) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await ws.send(json.dumps({"type": "heartbeat", "agent_id": AGENT_ID}))
        except Exception:
            break


async def listen_server(ws) -> None:
    """Принимает команды сервера: assign и abort."""

    global current_job

    async for message in ws:
        data = json.loads(message)

        if data["type"] == "assign":
            # На всякий случай завершаем старую задачу, если сервер назначил новую.
            if current_job and not current_job.done():
                current_job.cancel()

            current_job = asyncio.create_task(process_task(data, ws))

        elif data["type"] == "abort":
            print(f"[{AGENT_ID}] Abort command for task {data['task_id']}")

            if data.get("task_id") == current_task_id:
                if current_job and not current_job.done():
                    current_job.cancel()


async def main() -> None:
    while True:
        try:
            async with websockets.connect(SERVER_WS_URL) as ws:
                await ws.send(json.dumps({"type": "register", "agent_id": AGENT_ID}))
                response = await ws.recv()
                print(f"[{AGENT_ID}] Registered: {response}")

                heartbeat_task = asyncio.create_task(heartbeat(ws))

                try:
                    await listen_server(ws)
                finally:
                    heartbeat_task.cancel()

        except websockets.exceptions.ConnectionClosed:
            print(f"[{AGENT_ID}] Reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as error:
            print(f"[{AGENT_ID}] Error: {error}, reconnecting...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
