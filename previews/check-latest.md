## Проверка подключений

- ✅ Конфигурация: 30 стр.: FR 9 · IT 8 · EN 11 · PT 2; все страницы на месте
- ✅ Часы публикаций (Asia/Yerevan): 00:00, 12:00, 15:00, 18:00, 21:00 · расписание выключено
- ✅ Секрет FB_APP_ID задан
- ✅ Секрет FB_APP_SECRET задан
- ✅ Токены Facebook: расшифрованы, страниц: 24
- ✅ Facebook: названия отличаются от Make (ID совпадают, это не ошибка): Momenti da Ricordare → сейчас « Momenti da Ricordare»; Whispers After Midnight → сейчас «Whispers After Midnight

»; The Door She Closed → сейчас «The Door She Closed

»
- ✅ Источник историй: найдена следующая история — пока новых нет (опубликуйте историю с фото на любой из страниц или добавьте ссылку в очередь)
- ❌ WordPress https://1tari.ru (FR+PT, IT, EN): ключ Make Connector (WP_API_KEY) НЕ задан
- ❌ Секрет Claude (CLAUDE_CODE_OAUTH_TOKEN или ANTHROPIC_API_KEY) НЕ задан
- ❌ Facebook: проблемы со страницами: Les Vérités du Cœur — нет токена; La Vie qui Passe — нет токена; Verità Mai Confessate — нет токена; Promises in Ashes — нет токена; The Life Edit — нет токена; Segredos que a Vida Guarda — нет токена
- ❌ WordPress 1tari.ru: GET users/me: You are not currently logged in. — хостинг, похоже, вырезает заголовок авторизации. Решение: секрет WP_API_KEY с ключом плагина Make Connector (WordPress → Make → API Key).
- ❌ Claude: не задан ни CLAUDE_CODE_OAUTH_TOKEN, ни ANTHROPIC_API_KEY
