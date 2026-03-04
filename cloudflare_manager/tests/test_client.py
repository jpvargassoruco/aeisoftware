import unittest
from unittest.mock import patch, MagicMock
from cloudflare_manager.client import CloudflareTunnelManager

class TestCloudflareTunnelManager(unittest.TestCase):

    def setUp(self):
        self.manager = CloudflareTunnelManager(
            api_token="test_token",
            account_id="test_account",
            zone_id="test_zone"
        )
        self.tunnel_id = "test_tunnel_id"

    @patch('cloudflare_manager.client.requests.get')
    def test_get_tunnel_config(self, mock_get):
        # Arrange
        mock_response = MagicMock()
        mock_response.json.return_value = {"result": {"config": {"ingress": []}}}
        mock_get.return_value = mock_response

        # Act
        result = self.manager.get_tunnel_config(self.tunnel_id)

        # Assert
        mock_get.assert_called_once()
        self.assertEqual(result, {"config": {"ingress": []}})

    @patch('cloudflare_manager.client.requests.put')
    def test_update_tunnel_config(self, mock_put):
        # Arrange
        mock_response = MagicMock()
        mock_response.json.return_value = {"result": {"success": True}}
        mock_put.return_value = mock_response
        new_config = {"config": {"ingress": [{"service": "http_status:404"}]}}

        # Act
        result = self.manager.update_tunnel_config(self.tunnel_id, new_config)

        # Assert
        mock_put.assert_called_once()
        self.assertEqual(result, {"success": True})

    @patch('cloudflare_manager.client.CloudflareTunnelManager.get_tunnel_config')
    @patch('cloudflare_manager.client.CloudflareTunnelManager.update_tunnel_config')
    def test_add_route_to_tunnel_success(self, mock_update, mock_get):
        # Arrange
        current_config = {
            "config": {
                "ingress": [
                    {"hostname": "existing.com", "service": "http://localhost:8069"},
                    {"service": "http_status:404"}
                ]
            }
        }
        mock_get.return_value = current_config
        
        # Act
        result = self.manager.add_route_to_tunnel(self.tunnel_id, "new.com", "http://localhost:3000")

        # Assert
        self.assertTrue(result)
        mock_update.assert_called_once()
        # Verify the payload passed to update has the new rule BEFORE the catch-all
        updated_payload = mock_update.call_args[0][1]
        rules = updated_payload['config']['ingress']
        self.assertEqual(len(rules), 3)
        self.assertEqual(rules[1]['hostname'], "new.com")
        self.assertEqual(rules[2]['service'], "http_status:404")

    @patch('cloudflare_manager.client.CloudflareTunnelManager.get_tunnel_config')
    @patch('cloudflare_manager.client.CloudflareTunnelManager.update_tunnel_config')
    def test_add_route_already_exists(self, mock_update, mock_get):
        # Arrange
        current_config = {
            "config": {
                "ingress": [
                    {"hostname": "existing.com", "service": "http://localhost:8069"},
                    {"service": "http_status:404"}
                ]
            }
        }
        mock_get.return_value = current_config
        
        # Act
        result = self.manager.add_route_to_tunnel(self.tunnel_id, "existing.com", "http://localhost:8069")

        # Assert
        self.assertTrue(result)
        mock_update.assert_not_called()  # Should not update if it exists

if __name__ == '__main__':
    unittest.main()
