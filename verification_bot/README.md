# Standalone Verification Bot

An ultra-lightweight, standalone Discord Verification & Onboarding bot with zero heavy dependencies.

## Features
- **Click-to-Verify Button**: `/setup-verify` posts a persistent green **Verify** button.
- **Manual Verification**: `/verify @member` for staff.
- **Join Guard Alt Protection**: Blocks new alt accounts (account age check & default avatar check).
- **Auto Roles & Welcome Messages**: Automatically welcomes users and grants default roles.
- **Dashboard Connected**: Automatically reads `database/onboarding_configs.json`.

## Deployment Instructions

### 1. Requirements
Install dependencies:
```bash
pip install -r requirements.txt
```

### 2. Configure Token
Create a `.env` file containing:
```env
DISCORD_BOT_TOKEN=your_bot_token_here
```

### 3. Run
```bash
python main.py
```
