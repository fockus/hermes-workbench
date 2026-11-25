# Происхождение и лицензии

**Упаковочный слой Hermes Workbench** (установщик, launchers, тесты, lock-файл и документация) — MIT, `Copyright (c) 2026 Hermes Workbench contributors`, см. [LICENSE](LICENSE). Это коллективное обозначение авторов упаковки в истории проекта; год соответствует первому коммиту. Git-аккаунт или настройка `user.name` не подтверждают передачу исключительных прав одному человеку. Workbench не издан от имени Nous Research и не является автором перечисленных ниже компонентов.

| Материал | Публичный источник и права |
|---|---|
| Hermes Agent, в том числе исходные фрагменты в опциональном TUI-патче | [NousResearch/hermes-agent, `v0.23.3`](https://github.com/NousResearch/hermes-agent/tree/caafe5389ae585ef77387a962aee05b1bcf17029). `Copyright (c) 2025 Nous Research`, [MIT](LICENSES/Hermes-Agent.txt). |
| Memory Bank: runtime, skill, команды и публичный `hermes/memory-bank` | [fockus/skill-memory-bank](https://github.com/fockus/skill-memory-bank/tree/c0e56506df71bd1d066ff77870382797a4bf611b). `Copyright (c) 2026 Anton Ivanov`, [MIT](LICENSES/Memory-Bank.txt). Патч Workbench заменяет поиск банка по рабочему каталогу на фиксированный `profile/.memory-bank` и ограничивает рабочий каталог контекстного хука. |
| Отладочные материалы внутри Memory Bank | `agents/mb-debugger.md`, `references/debugging/` и `scripts/mb-find-polluter.sh` сохраняют upstream notices о материалах [obra/superpowers 5.1.0](https://github.com/obra/superpowers/tree/f2cbfbefebbfef77321e4c9abc9e949826bea9d7). `Copyright (c) 2025 Jesse Vincent`, [MIT](LICENSES/Superpowers.txt). |
| LLM Wiki: runtime CLI и lock зависимостей | [fockus/skill-llm-wiki](https://github.com/fockus/skill-llm-wiki/tree/99fea439401fcd079150db571a4650250a47943a). `Copyright (c) 2026 Anton Ivanov`, [MIT](LICENSES/LLM-Wiki.txt). Исполняется отдельно в Python 3.11; его JSON CLI используют и человек, и нативный плагин. |
| Нативный `plugins/llm-wiki` | Адаптирован из `fockus/skill-llm-wiki`, Git-коммит `c5bdf680393a429d857397d03918406c2d0260cf`, только два файла `hermes/llm-wiki/{__init__.py,plugin.yaml}`. `Copyright (c) 2026 Anton Ivanov`, [MIT](LICENSES/LLM-Wiki.txt). Workbench привязал корень к профилю, заменил конфигурируемый runtime на launcher этой установки и выбрал полнотекстовый поиск по умолчанию. |

Установщик получает исходники по Git и сверяет лицензионные тексты; зависимости сохраняют собственные права. Веса embedding-модели не поставляются и не перелицензируются. `LICENSE` Workbench не заменяет сторонние notices.

**Проверка происхождения Wiki-адаптера.** Указанный Git-объект — коммит с деревом `756eba28173f68a04ce6d03299c92caa8edc071a`. Идентификаторы двух исходных blobs записаны в lock. Его корневой `LICENSE` (blob `1ae9ed05bf5e47168cb9fd035beb291c5ae9ec9b`) совпал с MIT-лицензией публичного runtime. В поставку входят изменённые файлы адаптера и notice, не ветка, Git-история, экспорт Wiki или данные автора. Публичного тега с этим адаптером не предполагается.

**Палитра и футер.** Палитра `skins/claude-graphite.yaml` и локальные изменения футера в `patches/status-rule.patch` предоставлены для распространения по MIT. Для заимствованного кода Hermes сохраняется notice Nous Research. Имя темы не означает аффилированность; разрешение не распространяется на частные плагины, конфигурацию или данные.
