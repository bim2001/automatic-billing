# Smart Energy Monitoring System

## 📋 Overview
A complete energy monitoring and billing system for apartment buildings with IoT integration.

## 🚀 Features

### Week 1: Data Foundation
- EnergyUsage model for storing meter readings
- REST API for IoT devices (ESP32)
- Batch reading support

### Week 2: Automatic Billing
- Auto-generate monthly bills from energy usage
- Due date calculation (end of month)
- Billing history per tenant

### Week 3: Monitoring Dashboard
- Tenant: Daily consumption graph
- Tenant: Month-over-month comparison
- Owner: Building-wide statistics
- Owner: Top consuming rooms

### Week 4: Smart Features
- Abnormal usage detection (statistical)
- High consumption alerts (near limit)
- Late payment penalties
- Automated alerts system

### Week 5: Automation
- Daily tasks management command
- Email reminders (configurable days before due)
- IoT simulator for testing
- Windows Task Scheduler integration
- Complete test suite

### Week 6: Polishing
- System settings page (configurable rates)
- Contact information management
- Final documentation

## 🔧 Installation

1. Clone repository
2. Create virtual environment:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate