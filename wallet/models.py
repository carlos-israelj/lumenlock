from django.db import models

# Create your models here.
class Wallet(models.Model):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    public_key = models.CharField(max_length=56)
    secret_seed = models.CharField(max_length=200)  # Fixed: encrypted strings are ~151 chars
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Prevent duplicate wallets for same user (race condition protection)
        constraints = [
            models.UniqueConstraint(fields=['user'], name='unique_user_wallet')
        ]

    def __str__(self):
        return self.user.username + ' - ' + self.public_key