import requests
from requests.auth import HTTPBasicAuth
import json
import time
from typing import Dict, List, Optional, Any
from markdown import markdown
from html import escape
import logging
import os
import sys

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def check_dependencies():
    """Проверяет наличие всех необходимых зависимостей."""
    required = {
        'requests': ('2.31.0', 'HTTP-запросы к API'),
        'markdown': ('3.4.4', 'Преобразование Markdown в HTML'),
        'python-dotenv': ('1.0.0', 'Загрузка переменных окружения')
    }
    
    missing = []
    for package, (min_version, purpose) in required.items():
        try:
            importlib.import_module(package)
            installed_version = pkg_resources.get_distribution(package).version
            if pkg_resources.parse_version(installed_version) < pkg_resources.parse_version(min_version):
                missing.append(f"{package}>={min_version} (установлено {installed_version}) - {purpose}")
        except ImportError:
            missing.append(f"{package}>={min_version} - {purpose}")
    
    if missing:
        logger.error("Отсутствуют необходимые зависимости:")
        for dep in missing:
            logger.error(f"  - {dep}")
        logger.error("\nУстановите зависимости командой:\n  pip install -r requirements.txt")
        sys.exit(1)

def create_default_requirements():
    """Создает файл requirements.txt по умолчанию."""
    default_content = """# Основные зависимости
requests>=2.31.0
markdown>=3.4.4
python-dotenv>=1.0.0

# Дополнительные зависимости для разработки
pylint>=2.17.0
mypy>=1.3.0
pytest>=7.3.1
"""
    with open("requirements.txt", "w", encoding="utf-8") as f:
        f.write(default_content)
    logger.info("Создан файл requirements.txt с зависимостями по умолчанию")

def load_config(config_path: str = "config.json") -> Dict[str, Any]:
    """Загружает конфигурационный файл и проверяет обязательные ключи."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            required_keys = ["jira", "confluence", "deepseek", "epic_key"]
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Отсутствует обязательный ключ конфигурации: {key}")
            return config
    except FileNotFoundError:
        raise FileNotFoundError(f"Конфигурационный файл {config_path} не найден")
    except json.JSONDecodeError:
        raise ValueError(f"Неверный формат JSON в файле {config_path}")

def get_epic_info(epic_key: str) -> Dict[str, Any]:
    """Получает информацию об эпике из Jira."""
    url = f"{CONFIG['jira']['url']}/rest/api/3/issue/{epic_key}?fields=summary,description"
    try:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(CONFIG["jira"]["api_user"], CONFIG["jira"]["api_token"]),
            timeout=30
        )
        response.raise_for_status()
        
        fields = response.json().get("fields", {})
        return {
            "key": epic_key,
            "summary": fields.get("summary", ""),
            "description": fields.get("description", "")
        }
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Ошибка подключения к Jira: {str(e)}")

def get_jira_issues(epic_key: str) -> List[Dict[str, Any]]:
    """Получает все задачи, связанные с эпиком."""
    jql = f'\"Epic Link\" = {epic_key} OR \"Parent Link\" = {epic_key}'
    fields = "summary,status,assignee,updated,description,comment,issuetype,priority"
    start_at = 0
    max_results = 100
    all_issues = []
    
    while True:
        url = f"{CONFIG['jira']['url']}/rest/api/3/search?jql={jql}&fields={fields}&startAt={start_at}&maxResults={max_results}"
        try:
            response = requests.get(
                url,
                auth=HTTPBasicAuth(CONFIG["jira"]["api_user"], CONFIG["jira"]["api_token"]),
                timeout=30
            )
            response.raise_for_status()
            
            data = response.json()
            all_issues.extend(data.get("issues", []))
            
            if start_at + max_results >= data.get("total", 0):
                break
            start_at += max_results
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Ошибка подключения к Jira: {str(e)}")
    
    return sorted(
        all_issues,
        key=lambda x: x.get("fields", {}).get("updated", ""),
        reverse=True
    )

def analyze_with_deepseek(issues: List[Dict[str, Any]], epic_summary: str) -> str:
    """Анализирует задачи с помощью DeepSeek API."""
    headers = {
        "Authorization": f"Bearer {CONFIG['deepseek']['api_key']}",
        "Content-Type": "application/json"
    }
    
    tasks_text = "\n".join(
        f"{i+1}. {issue.get('key', 'N/A')} ({issue.get('fields', {}).get('issuetype', {}).get('name', 'N/A')}): "
        f"{issue.get('fields', {}).get('summary', 'N/A')} "
        f"(Status: {issue.get('fields', {}).get('status', {}).get('name', 'N/A')}, "
        f"Priority: {issue.get('fields', {}).get('priority', {}).get('name', 'N/A')})"
        for i, issue in enumerate(issues[:50])
    
    prompt = {
        "model": CONFIG["deepseek"]["model"],
        "messages": [{
            "role": "user",
            "content": f"""Проанализируй задачи из эпика "{epic_summary}":
            {tasks_text}
            
            Предоставь детальный анализ:
            1. Общий прогресс (%)
            2. Критические проблемы/блокеры
            3. Не назначенные задачи
            4. Рекомендации по приоритизации
            5. Оценка рисков"""
        }]
    }
    
    try:
        response = requests.post(
            CONFIG["deepseek"]["api_url"],
            headers=headers,
            json=prompt,
            timeout=60
        )
        response.raise_for_status()
        return response.json().get("choices", [{}])[0].get("message", {}).get("content", "Анализ недоступен")
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка DeepSeek API: {str(e)}")
        return "Анализ временно недоступен"

def find_confluence_page(epic_key: str) -> Optional[Dict[str, Any]]:
    """Ищет страницу в Confluence по ключу эпика."""
    url = f"{CONFIG['confluence']['url']}/rest/api/content?spaceKey={CONFIG['confluence']['space_key']}&title=Статус эпика {epic_key}"
    try:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(CONFIG["confluence"]["api_user"], CONFIG["confluence"]["api_token"]),
            timeout=30
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        return results[0] if results else None
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Ошибка подключения к Confluence: {str(e)}")

def update_confluence_page(existing_page: Optional[Dict[str, Any]], content: str, epic_info: Dict[str, Any]) -> Dict[str, Any]:
    """Обновляет или создает страницу в Confluence."""
    page_title = f"Статус эпика {epic_info['key']} - {epic_info['summary']}"
    api_url = CONFIG["confluence"]["url"]
    auth = HTTPBasicAuth(CONFIG["confluence"]["api_user"], CONFIG["confluence"]["api_token"])
    
    data = {
        "type": "page",
        "title": page_title,
        "body": {
            "storage": {
                "value": content,
                "representation": "storage"
            }
        }
    }
    
    if existing_page:
        url = f"{api_url}/rest/api/content/{existing_page['id']}"
        data["version"] = {"number": existing_page["version"]["number"] + 1}
        method = "PUT"
    else:
        url = f"{api_url}/rest/api/content"
        data["space"] = {"key": CONFIG["confluence"]["space_key"]}
        method = "POST"
    
    try:
        response = requests.request(
            method,
            url,
            auth=auth,
            headers={"Content-Type": "application/json"},
            json=data,
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Ошибка Confluence API: {str(e)}")

def generate_content(issues: List[Dict[str, Any]], analysis: str, epic_info: Dict[str, Any]) -> str:
    """Генерирует HTML-контент для страницы Confluence."""
    last_update = time.strftime("%Y-%m-%d %H:%M:%S")
    
    styles = """
    <style>
        .task-table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 20px;
        }
        .task-table th, .task-table td {
            border: 1px solid #ddd;
            padding: 8px;
            vertical-align: top;
        }
        .task-table th {
            background-color: #f2f2f2;
            text-align: left;
        }
        .task-description {
            margin-top: 5px;
            color: #555;
            font-size: 0.9em;
        }
        .task-type {
            display: inline-block;
            padding: 2px 5px;
            border-radius: 3px;
            font-size: 0.8em;
            margin-right: 5px;
        }
        .task-priority {
            display: inline-block;
            padding: 2px 5px;
            border-radius: 3px;
            font-size: 0.8em;
        }
        .analysis-block {
            background-color: #f8f9fa;
            padding: 15px;
            border-radius: 5px;
            margin: 20px 0;
            border-left: 4px solid #4285f4;
        }
        .status-done { color: #4CAF50; font-weight: bold; }
        .status-inprogress { color: #2196F3; font-weight: bold; }
        .status-open { color: #9E9E9E; font-weight: bold; }
        .priority-high { background-color: #FFCDD2; color: #C62828; }
        .priority-medium { background-color: #FFF9C4; color: #F9A825; }
        .priority-low { background-color: #C8E6C9; color: #388E3C; }
    </style>
    """
    
    header = f"""
    <h1>Статус эпика: {escape(epic_info['key'])} - {escape(epic_info['summary'])}</h1>
    <div class="task-description">{markdown(escape(epic_info.get('description', 'Описание отсутствует')))}</div>
    """
    
    tasks_table = """
    <h2>Задачи</h2>
    <table class="task-table">
        <tr>
            <th style="width: 10%">Ключ</th>
            <th style="width: 45%">Детали</th>
            <th style="width: 15%">Статус</th>
            <th style="width: 15%">Исполнитель</th>
            <th style="width: 15%">Обновлено</th>
        </tr>
    """
    
    for issue in issues:
        fields = issue.get("fields", {})
        key = issue.get("key", "N/A")
        summary = escape(fields.get("summary", "N/A"))
        status = escape(fields.get("status", {}).get("name", "N/A"))
        issue_type = escape(fields.get("issuetype", {}).get("name", "N/A"))
        priority = escape(fields.get("priority", {}).get("name", "N/A"))
        assignee = escape(fields.get("assignee", {}).get("displayName", "Не назначен")) if fields.get("assignee") else "Не назначен"
        updated = escape(fields.get("updated", "")[:10])
        description = escape(fields.get("description", ""))
        
        status_class = ""
        if "готов" in status.lower():
            status_class = "status-done"
        elif "прогрес" in status.lower():
            status_class = "status-inprogress"
        else:
            status_class = "status-open"
            
        priority_class = ""
        if "высок" in priority.lower():
            priority_class = "priority-high"
        elif "средн" in priority.lower():
            priority_class = "priority-medium"
        else:
            priority_class = "priority-low"
        
        tasks_table += f"""
        <tr>
            <td><a href="{CONFIG['jira']['url']}/browse/{key}" target="_blank">{key}</a></td>
            <td>
                <div>
                    <span class="task-type">{issue_type}</span>
                    <span class="{priority_class}">{priority}</span>
                </div>
                <strong>{summary}</strong>
                <div class="task-description">{markdown(description) if description else 'Нет описания'}</div>
            </td>
            <td class="{status_class}">{status}</td>
            <td>{assignee}</td>
            <td>{updated}</td>
        </tr>
        """
    
    tasks_table += "</table>"
    
    analysis_block = f"""
    <div class="analysis-block">
        <h2>Анализ выполнения</h2>
        {analysis.replace('\n', '<br>')}
    </div>
    """
    
    footer = f"""
    <div style="margin-top: 30px; color: #777; font-size: 0.9em;">
        <p>Последнее обновление: {last_update}</p>
        <p>Страница автоматически сгенерирована</p>
    </div>
    """
    
    return styles + header + tasks_table + analysis_block + footer

def main():
    """Основная функция выполнения скрипта."""
    try:
        # Проверка зависимостей
        try:
            import importlib
            import pkg_resources
        except ImportError:
            logger.error("Необходимые модули для проверки зависимостей не установлены")
            if not os.path.exists("requirements.txt"):
                create_default_requirements()
            logger.info("Пожалуйста, установите зависимости командой: pip install -r requirements.txt")
            sys.exit(1)
        
        check_dependencies()
        
        if not os.path.exists("requirements.txt"):
            create_default_requirements()
        
        epic_key = CONFIG["epic_key"]
        logger.info(f"Обработка эпика: {epic_key}")
        
        epic_info = get_epic_info(epic_key)
        issues = get_jira_issues(epic_key)
        analysis = analyze_with_deepseek(issues, epic_info["summary"])
        content = generate_content(issues, analysis, epic_info)
        page = find_confluence_page(epic_key)
        result = update_confluence_page(page, content, epic_info)
        
        logger.info(f"Успешно! Страница доступна по адресу: {CONFIG['confluence']['url']}{result['_links']['webui']}")
    
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()