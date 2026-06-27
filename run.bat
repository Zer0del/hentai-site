@echo off
echo Starting FAKKU Manga Reader...
echo.
echo Рекомендуется установить зависимости (один раз):
echo   pip install -r requirements.txt
echo   (flask + Pillow для миниатюр)
echo.
echo ================================================
echo  ВАЖНО: 
echo  1. Чтобы остановить сервер — нажми Ctrl+C в ЭТОМ окне
echo     и дождись сообщения "Shutting down..."
echo  2. НЕ просто закрывай окно — процесс может остаться жить!
echo  3. После остановки можно закрывать окно.
echo ================================================
echo.
python app.py
echo.
echo Server stopped.
pause