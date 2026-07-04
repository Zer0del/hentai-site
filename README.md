# Hentach - Hentai Manga Reader

Полнофункциональный сайт для чтения хентай-манги (Hentach).

## Возможности

- **Главная страница** с красивой сеткой обложек
- **Страница манги**: большая обложка, теги, рейтинг 1-5, описание, кнопка Читать
- **Высококачественная читалка**:
  - Навигация клавишами (стрелки, A/D, пробел)
  - Зум колесом мыши + перетаскивание
  - Режимы: 1 страница / 2 страницы
  - Fit: по ширине / высоте / оригинал
  - Направление LTR / RTL
  - Полоса миниатюр + прогресс
  - Тач-свайп поддержка
- **Админ**: добавление манги (через модалку на главной или /admin)
  - Загрузка обложки
  - Загрузка множества изображений или ZIP/CBZ
- Оценки манги 1–5 звёзд (сохраняются)
- Поиск + фильтрация по тегам
- Полностью локально, без внешних зависимостей после запуска

## Запуск

1. Убедитесь, что Python установлен.
2. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
   
   Или вручную:
   ```bash
   pip install flask Pillow requests
   ```
   
   **Pillow обязателен** для генерации миниатюр (обложки и превью страниц). Без него будет предупреждение в консоли, и миниатюры не будут создаваться (используются оригинальные большие изображения).
3. Запустите:
   ```bash
   python app.py
   ```
4. Откройте http://127.0.0.1:5000 на этом компьютере.

## Как проверить на телефоне

1. Телефон и компьютер должны быть подключены к **одной Wi-Fi сети**.
2. На компьютере запусти сервер (уже слушает все устройства).
3. Открой командную строку (Win + R → cmd) и выполни команду:
   ```
   ipconfig
   ```
4. Найди блок **Wireless LAN adapter Wi-Fi** (или Ethernet) и скопируй **IPv4-адрес** (обычно 192.168.x.x или 10.0.x.x).
5. На телефоне открой браузер и введи:
   ```
   http://ТВОЙ_IP:5000
   ```
   Пример: `http://192.168.1.105:5000`

**Советы:**
- Если не открывается — временно отключи брандмауэр Windows для теста.
- Можно также использовать эмуляцию в Chrome: F12 → переключить на мобильный вид (Toggle Device Toolbar).
- Для доступа из интернета (не обязательно) можно использовать ngrok.

**Пароль администратора** по умолчанию: `admin123`

## Проверка зависимостей

Чтобы проверить, установлен ли Pillow (нужен для миниатюр):

```powershell
python -m pip show Pillow
```

Или так:

```powershell
python -c "from PIL import Image; print('Pillow version:', Image.__version__)"
```

Если выдаст ошибку "No module named PIL" — нужно установить.

Запусти сервер — если Pillow не установлен, увидишь предупреждение в консоли.

Можно изменить в `app.py` в строке `ADMIN_PASS`.

## Как добавить мангу (админ)

- Нажмите большую красную кнопку **«Добавить мангу»** на главной
- Или перейдите на /admin
- Введите пароль
- Загрузите обложку + страницы (или .zip/.cbz)
- Готово. Манга сразу появится

## Структура файлов

```
uploads/manga/<slug>/
  cover-xxx.jpg
  001.jpg
  002.jpg
  ...
data/manga.db
```

## Production deployment (VPS with Nginx + Gunicorn)

For deployment on VPS (like the hentach.ru setup):

1. Use gunicorn:
   ```bash
   gunicorn -w 4 --bind 0.0.0.0:8000 app:app
   ```

2. Nginx config (see `nginx.conf` in project root for full example):
   - Set `client_max_body_size 2G;` to allow large manga uploads (many pages or big ZIPs).
   - Proxy / to gunicorn.
   - Serve /uploads and /static directly from nginx for speed.

3. Important: the 413 error on /api/add_manga means your reverse proxy (Nginx) has a low upload limit. Use the included `nginx.conf`.

4. Set env vars:
   - HENTACH_ADMIN_PASS=secret (or FAKKU_ADMIN_PASS for compat)
   - ALLOWED_IPS= (if using IP whitelist, but removed)

5. For large uploads, make sure disk space and memory are sufficient. Use ZIP for many pages.

The Tailwind CDN warning is harmless for this project (it's dev convenience); for full prod you can build Tailwind, but not required.
```

## Качество читалки

Читалка построена с нуля специально под классическую мангу:
- Оптимальные зоны клика
- Плавный зум + панорамирование
- Отличная клавиатурная навигация
- Поддержка двойной страницы
- Полностью кастомизируемые режимы просмотра

## Стиль

Сайт для личной библиотеки:
- Тёмная тема
- Акцентный цвет #e11d48 (розово-красный)
- Чистые карточки с хорошими hover-эффектами
- Красивая типографика

Готов к использованию.

---

Создано для личного использования. Не используйте для распространения защищённого авторским правом контента.
