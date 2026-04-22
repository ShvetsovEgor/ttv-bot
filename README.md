# ttv-bot


https://aistudio.yandex.ru/docs/ru/ai-studio/operations/agents/manage-context.html

https://aistudio.yandex.ru/docs/ru/ai-studio/operations/agents/create-filesearch-text-agent.html

Работа с логами (journalctl)
journalctl -u ttv-bot -f — Просмотр логов в реальном времени. Позволяет видеть новые сообщения от бота сразу после их появления (как в консоли при локальном запуске).

journalctl -u ttv-bot -n 100 — Вывод последних 100 строк истории логов. Помогает быстро просмотреть последние события, если бот упал.

journalctl -u ttv-bot --since today — Отображение всех логов, накопленных с начала текущих суток.

journalctl -u ttv-bot --no-pager — Вывод логов без сокращений и разбивки на страницы. Удобно, если нужно скопировать длинную ошибку целиком.

Управление службой (systemctl)
sudo systemctl start ttv-bot — Запуск бота. Используется для первого старта или после ручной остановки.

sudo systemctl stop ttv-bot — Плановая остановка бота. Служба корректно завершит текущие процессы.

sudo systemctl restart ttv-bot — Перезапуск бота. Обязательная команда после выполнения git pull, чтобы изменения в коде вступили в силу.

sudo systemctl status ttv-bot — Проверка текущего статуса. Покажет, работает ли бот сейчас (active), сколько памяти потребляет и не возникло ли ошибок при запуске.

sudo systemctl enable ttv-bot — Включение автозапуска. Бот будет автоматически стартовать сразу после включения или перезагрузки виртуальной машины.

sudo systemctl disable ttv-bot — Отключение автозапуска. Бот перестанет автоматически включаться вместе с сервером.

sudo systemctl daemon-reload — Перезагрузка конфигурации systemd. Эту команду нужно вводить каждый раз, если ты вносил изменения в файл /etc/systemd/system/ttv-bot.service.

sudo systemctl kill ttv-bot — Принудительное завершение. Используется в крайнем случае, если бот завис и не реагирует на команду stop.