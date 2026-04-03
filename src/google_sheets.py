import json
import logging
import urllib.request
import urllib.error
from datetime import datetime

logger = logging.getLogger("google_sheets")


class GoogleSheetsLogger:
    @staticmethod
    def log_account(
        webhook_url: str,
        email: str,
        total_points: int,
        earned_today: int,
        pc_search: int,
        mobile_search: int,
        offers: int
    ) -> bool:
        """
        Sends account execution summary to a Google Apps Script Webhook.
        """
        if not webhook_url:
            return False

        date_str = datetime.now().strftime("%d/%m/%Y")
        
        payload = {
            "date": date_str,
            "email": email,
            "total_points": total_points,
            "pc_search": pc_search,
            "mobile_search": mobile_search,
            "today_points": earned_today,
            "offers": offers
        }
        
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                webhook_url, 
                data=data, 
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                result = response.read().decode("utf-8")
                if "Success" in result or response.status == 200:
                    logger.info("Successfully pushed log to Google Sheets Webhook.")
                    return True
                else:
                    logger.warning(f"Google Sheets Webhook returned unexpected status: {result}")
        except urllib.error.URLError as e:
            logger.error(f"Failed to connect to Google Sheets Webhook: {e}")
        except Exception as e:
            logger.error(f"Google Sheets Webhook error: {e}")

        return False
