@echo off
echo Starting Django Server...
start cmd /k "cd C:\Users\User\Desktop_Local\appartment\apartsystem && .venv\Scripts\activate && python manage.py runserver"
timeout /t 3
echo Starting ngrok...
start cmd /k "cd C:\Users\User\Desktop_Local\appartment && ngrok http 8000"
echo Both servers are running!
echo Django: http://127.0.0.1:8000
echo ngrok: https://wobbly-vividness-company.ngrok-free.dev
echo ngrok Inspector: http://127.0.0.1:4040
pause