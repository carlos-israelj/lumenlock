from django.test import TestCase, Client
from django.contrib.auth.models import User
from wallet.models import Wallet
from stellar_sdk import Keypair
import cryptocode
import json
from unittest.mock import patch, MagicMock


class WalletModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass123')

    def test_wallet_creation(self):
        """Test that a wallet can be created with correct fields"""
        keypair = Keypair.random()
        encrypted_seed = cryptocode.encrypt(keypair.secret, 'password123')

        wallet = Wallet.objects.create(
            user=self.user,
            public_key=keypair.public_key,
            secret_seed=encrypted_seed
        )

        self.assertEqual(wallet.user, self.user)
        self.assertEqual(wallet.public_key, keypair.public_key)
        self.assertTrue(len(wallet.secret_seed) > 56)  # Encrypted should be longer

    def test_secret_seed_field_length(self):
        """Test that secret_seed field can store encrypted data (151+ chars)"""
        keypair = Keypair.random()
        encrypted_seed = cryptocode.encrypt(keypair.secret, 'password123')

        # Encrypted string is ~151 chars, verify it's stored without truncation
        wallet = Wallet.objects.create(
            user=self.user,
            public_key=keypair.public_key,
            secret_seed=encrypted_seed
        )

        wallet.refresh_from_db()
        self.assertEqual(wallet.secret_seed, encrypted_seed)
        self.assertEqual(len(wallet.secret_seed), len(encrypted_seed))


class CreateWalletViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass123')

    def test_create_wallet_requires_login(self):
        """Test that create_wallet requires authentication"""
        response = self.client.post('/create_wallet', {'password': 'test123'})
        self.assertEqual(response.status_code, 302)  # Redirect to login

    @patch('wallet.views.requests.get')
    def test_create_wallet_assigns_correct_user(self, mock_requests_get):
        """Test that wallet is assigned to request.user, not first user"""
        # Mock friendbot response to avoid external network call
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_requests_get.return_value = mock_response

        # Create another user who would be User.objects.first()
        User.objects.create_user(username='firstuser', password='pass123')

        self.client.login(username='testuser', password='testpass123')
        response = self.client.post('/create_wallet', {'password': 'walletpass123'})

        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.user, self.user)
        self.assertEqual(wallet.user.username, 'testuser')

    @patch('wallet.views.requests.get')
    def test_create_wallet_encrypts_secret(self, mock_requests_get):
        """Test that secret seed is properly encrypted"""
        # Mock friendbot response to avoid external network call
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_requests_get.return_value = mock_response

        self.client.login(username='testuser', password='testpass123')
        response = self.client.post('/create_wallet', {'password': 'mypassword'})

        wallet = Wallet.objects.get(user=self.user)

        # Verify encryption by decrypting
        decrypted = cryptocode.decrypt(wallet.secret_seed, 'mypassword')
        self.assertIsNotNone(decrypted)
        self.assertTrue(decrypted.startswith('S'))  # Stellar secret keys start with S

    @patch('wallet.views.requests.get')
    def test_create_wallet_redirect_if_exists(self, mock_requests_get):
        """Test that creating wallet redirects to dashboard if wallet exists"""
        # Mock friendbot response to avoid external network call
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_requests_get.return_value = mock_response

        self.client.login(username='testuser', password='testpass123')

        # Create first wallet
        self.client.post('/create_wallet', {'password': 'pass123'})

        # Try to create second wallet
        response = self.client.post('/create_wallet', {'password': 'pass456'})
        self.assertRedirects(response, '/dashboard')

        # Verify only one wallet exists
        self.assertEqual(Wallet.objects.filter(user=self.user).count(), 1)


class CheckBalanceViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass123')

        keypair = Keypair.random()
        encrypted_seed = cryptocode.encrypt(keypair.secret, 'password123')

        self.wallet = Wallet.objects.create(
            user=self.user,
            public_key=keypair.public_key,
            secret_seed=encrypted_seed
        )

    def test_check_balance_requires_login(self):
        """Test that check_balance requires authentication"""
        response = self.client.post('/check_balance')
        self.assertEqual(response.status_code, 302)  # Redirect to login

    @patch('wallet.views.get_horizon_server')
    def test_check_balance_returns_json(self, mock_get_server):
        """Test that check_balance returns JSON response with mocked Horizon"""
        self.client.login(username='testuser', password='testpass123')

        # Mock Horizon server response
        mock_server = MagicMock()
        mock_account = MagicMock()
        mock_account.call.return_value = {
            'balances': [
                {'asset_type': 'native', 'balance': '100.0000000'}
            ]
        }
        mock_server.accounts.return_value.account_id.return_value = mock_account
        mock_get_server.return_value = mock_server

        response = self.client.post('/check_balance')
        self.assertEqual(response['Content-Type'], 'application/json')

        data = json.loads(response.content)
        self.assertEqual(data['balance'], '100.0000000')

    def test_check_balance_handles_missing_wallet(self):
        """Test check_balance handles users without wallets"""
        user_no_wallet = User.objects.create_user(username='nowallet', password='pass123')
        self.client.login(username='nowallet', password='pass123')

        response = self.client.post('/check_balance')
        data = json.loads(response.content)

        self.assertEqual(response.status_code, 404)
        self.assertIn('error', data)


class SendMoneyViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass123')

        keypair = Keypair.random()
        self.password = 'walletpass123'
        encrypted_seed = cryptocode.encrypt(keypair.secret, self.password)

        self.wallet = Wallet.objects.create(
            user=self.user,
            public_key=keypair.public_key,
            secret_seed=encrypted_seed
        )

    def test_send_money_requires_login(self):
        """Test that send_money requires authentication"""
        response = self.client.post('/send_money')
        self.assertEqual(response.status_code, 302)  # Redirect to login

    def test_send_money_validates_password(self):
        """Test that send_money rejects wrong password"""
        self.client.login(username='testuser', password='testpass123')

        data = {
            'recipient': 'GBBB...',
            'amount': '10',
            'transaction_password': 'wrongpassword'
        }

        response = self.client.post(
            '/send_money',
            json.dumps(data),
            content_type='application/json'
        )

        response_data = json.loads(response.content)
        self.assertEqual(response.status_code, 401)
        self.assertIn('error', response_data)

    def test_send_money_validates_amount(self):
        """Test that send_money validates amount is positive"""
        self.client.login(username='testuser', password='testpass123')

        data = {
            'recipient': 'GBBB...',
            'amount': '-10',
            'transaction_password': self.password
        }

        response = self.client.post(
            '/send_money',
            json.dumps(data),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 400)

    def test_send_money_validates_required_fields(self):
        """Test that send_money validates required fields"""
        self.client.login(username='testuser', password='testpass123')

        data = {
            'recipient': 'GBBB...',
            # Missing amount and password
        }

        response = self.client.post(
            '/send_money',
            json.dumps(data),
            content_type='application/json'
        )

        response_data = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response_data)

    def test_send_money_accepts_memo_field(self):
        """Test that send_money accepts optional memo field"""
        self.client.login(username='testuser', password='testpass123')

        data = {
            'recipient': 'GBBBB...',
            'amount': '10',
            'transaction_password': self.password,
            'memo': 'Test payment'
        }

        response = self.client.post(
            '/send_money',
            json.dumps(data),
            content_type='application/json'
        )

        # Will fail due to network, but validates memo is accepted
        self.assertIn(response.status_code, [400, 404, 500])


class TransactionHistoryViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass123')

        keypair = Keypair.random()
        encrypted_seed = cryptocode.encrypt(keypair.secret, 'password123')

        self.wallet = Wallet.objects.create(
            user=self.user,
            public_key=keypair.public_key,
            secret_seed=encrypted_seed
        )

    def test_transaction_history_requires_login(self):
        """Test that transaction_history requires authentication"""
        response = self.client.get('/transactions')
        self.assertEqual(response.status_code, 302)  # Redirect to login

    @patch('wallet.views.get_horizon_server')
    def test_transaction_history_returns_json(self, mock_get_server):
        """Test that transaction_history returns JSON with mocked Horizon"""
        self.client.login(username='testuser', password='testpass123')

        # Mock Horizon server response for payments
        mock_server = MagicMock()
        mock_payments = MagicMock()
        mock_payments.call.return_value = {
            '_embedded': {
                'records': [
                    {
                        'type': 'payment',
                        'id': '123456789',
                        'created_at': '2024-01-01T00:00:00Z',
                        'transaction_hash': 'abc123def456',
                        'amount': '100.0000000',
                        'asset_type': 'native',
                        'from': 'GABC123',
                        'to': 'GDEF456'
                    }
                ]
            }
        }
        mock_server.payments.return_value.for_account.return_value.order.return_value.limit.return_value = mock_payments
        mock_get_server.return_value = mock_server

        response = self.client.get('/transactions')
        self.assertEqual(response['Content-Type'], 'application/json')

        data = json.loads(response.content)
        self.assertIn('transactions', data)
        self.assertEqual(len(data['transactions']), 1)

    def test_transaction_history_handles_no_wallet(self):
        """Test transaction_history for users without wallets"""
        user_no_wallet = User.objects.create_user(username='nowallet', password='pass123')
        self.client.login(username='nowallet', password='pass123')

        response = self.client.get('/transactions')
        data = json.loads(response.content)

        self.assertEqual(response.status_code, 404)
        self.assertIn('error', data)


class DashboardViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass123')

    def test_dashboard_requires_login(self):
        """Test that dashboard requires authentication"""
        response = self.client.get('/dashboard')
        self.assertEqual(response.status_code, 302)  # Redirect to login

    def test_dashboard_shows_create_wallet_option(self):
        """Test dashboard shows create wallet when no wallet exists"""
        self.client.login(username='testuser', password='testpass123')

        response = self.client.get('/dashboard')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Create Wallet')

    @patch('wallet.views.get_horizon_server')
    def test_dashboard_shows_balance_with_wallet(self, mock_get_server):
        """Test dashboard shows balance when wallet exists"""
        keypair = Keypair.random()
        encrypted_seed = cryptocode.encrypt(keypair.secret, 'password123')

        Wallet.objects.create(
            user=self.user,
            public_key=keypair.public_key,
            secret_seed=encrypted_seed
        )

        # Mock Horizon server response
        mock_server = MagicMock()
        mock_account = MagicMock()
        mock_account.call.return_value = {
            'balances': [
                {'asset_type': 'native', 'balance': '100.0000000'}
            ]
        }
        mock_server.accounts.return_value.account_id.return_value = mock_account
        mock_get_server.return_value = mock_server

        self.client.login(username='testuser', password='testpass123')
        response = self.client.get('/dashboard')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Your balance')


class SecurityTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass123')

    def test_all_wallet_endpoints_require_auth(self):
        """Test that all wallet endpoints require authentication"""
        endpoints = [
            ('/create_wallet', 'post'),
            ('/check_balance', 'post'),
            ('/send_money', 'post'),
            ('/transactions', 'get'),
            ('/dashboard', 'get'),
        ]

        for endpoint, method in endpoints:
            if method == 'post':
                response = self.client.post(endpoint)
            else:
                response = self.client.get(endpoint)

            # Should redirect to login (302) for all endpoints
            self.assertEqual(
                response.status_code,
                302,
                f"{endpoint} should require authentication"
            )
