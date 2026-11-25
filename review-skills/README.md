# Выбрать и установить skills Superpowers

Из корня Workbench укажите абсолютный путь к своей доверенной установке этого выпуска (`--prefix "$WB"`, режим base или TUI). Для самого дополнения не нужны сеть, ключи или модель.

```sh
python3.14 -IB review-skills/review.py list
python3.14 -IB review-skills/review.py smoke --prefix "$WB" --all    # проба в одноразовом профиле
python3.14 -IB review-skills/review.py install --prefix "$WB" --all  # установка в $WB/profile
python3.14 -IB review-skills/review.py check --prefix "$WB" --all    # проверить файлы, каталог и slash-команды
```

| Выбор | Имена |
|---|---|
| Без флагов: ревью и завершение работы | `receiving-code-review`, `verification-before-completion`, `finishing-a-development-branch` |
| Дополнительно при `--all` | `brainstorming`, `diagnosing-superpowers`, `dispatching-parallel-agents`, `using-git-worktrees`, `using-superpowers`, `writing-plans` |

Для отдельного выбора вместо `--all` повторите `--skill ИМЯ` для каждого нужного skill. Та же форма выбора работает у `install`, `check` и `smoke`. Запустите новую сессию `"$WB/workbench"`: `/writing-plans` открывает установленные инструкции по составлению плана, но не устанавливает средства его исполнения. Для удаления используйте штатный Skills Hub. У каждого skill собственные имя, каталог и запись об установке.

## Что именно устанавливается

Источник — [obra/superpowers, коммит `22f0b463e7a5e579904dd6211a520321035526f7`](https://github.com/obra/superpowers/tree/22f0b463e7a5e579904dd6211a520321035526f7/skills). Девять **полных** каталогов: 42 исходных файла без местных правок, включая примеры, шаблоны, справочные материалы и скрипты. [Pins](pins.json) фиксируют идентификатор каждого Git-дерева, SHA-256 каждого файла, исполняемые биты и SHA-256 корневой лицензии. Установщик прикладывает к каждому каталогу [полный MIT notice](../LICENSES/Superpowers.txt), Copyright (c) 2025 Jesse Vincent. Репозиторий не содержит журналов сеансов, истории Git или конфигурации исходного профиля в составе дополнения.

Перед установкой проверяются receipt Workbench, коммит хоста и выбранные деревья. Весь выбор проходит штатный карантин и проверку политики `community` с результатом `safe` до установки первого нового skill. Чужая одноимённая копия или изменённые файлы приводят к отказу, а не к перезаписи. Установка под замком Skills Hub не является транзакцией на случай сбоя диска или песочницей ОС.

`check` сопоставляет байты и исполняемые биты, вызывает `skills_list`, `skill_view` и собирает настоящее slash-сообщение, **не отправляя его модели**. `smoke` дополнительно проверяет повторную установку и сохранность изменённой одноразовой копии. Установщик не запускает сервер визуализации из `brainstorming`, не читает чаты для `diagnosing-superpowers`, не запускает агентов или команды из текстов. Эти действия при дальнейшем использовании требуют отдельного решения пользователя; открытие визуального помощника может сделать запрос за внешним логотипом. Доставка каталога не доказывает исполнение его сценариев или полное соответствие другого профиля.

## Что не устанавливает отдельный `review-skills`

В едином профиле `python3.14 -IB install.py --prefix "$WB" --full-profile --with-tui --with-executors` эти девять деревьев входят в 57 выбранных из 61 проверенного дополнительного дерева. Не повторяйте их установку. Более широкий [skill pack](../skill-pack/README.md) поставляет `executing-plans` вместе с зависимостью и отдельную редакцию `writing-skills` без копии руководства Anthropic. Ограничения ниже относятся только к standalone-дополнению `review-skills`.

У восьми дополнительных кандидатов состав файлов, байты и исполняемые биты эталона совпали с полными каталогами на указанном коммите. Шесть поставляются выше. Для двух совпадения содержимого и допуска сканера оказалось недостаточно:

- `executing-plans`: его `scripts/task-start` и `scripts/task-done` требуют соседние `subagent-driven-development/scripts/{sdd-repo,task-brief,sdd-workspace}`. В [адаптации закреплённого Hermes](https://github.com/NousResearch/hermes-agent/tree/caafe5389ae585ef77387a962aee05b1bcf17029/optional-skills/software-development/subagent-driven-development) этих файлов нет. Дополнение не подменяет адаптацию и не устанавливает неработающую пару скриптов.
- `writing-skills`: `references/anthropic-best-practices.md` воспроизводит [руководство Anthropic](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices). Разрешение на распространение этой копии отдельно от корневой MIT-лицензии Superpowers не подтверждено. Каталог не урезан и целиком оставлен за пределами поставки.

Четыре адаптации Hermes — `requesting-code-review`, `systematic-debugging`, `test-driven-development`, `subagent-driven-development` — не дублируются и не перезаписываются.
