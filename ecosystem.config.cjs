module.exports = {
  apps: [
    {
      name: "ayrnews",
      script: "server.py",
      interpreter: "python3",
      args: "",
      cwd: "/opt/ayrnews",
      env: {
        PYTHONUNBUFFERED: "1",
      },
      max_restarts: 10,
      restart_delay: 5000,
      autorestart: true,
      watch: false,
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
  ],
};
