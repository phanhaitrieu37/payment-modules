"""Default reference generator: named prefix plus a CSPRNG suffix from the profile alphabet."""

from __future__ import annotations

import secrets

from payment_module.domain.reference import PaymentReference, ReferenceProfile


class RandomSuffixGenerator:
    """Collisions are possible; the database unique constraint decides and the application
    retries a bounded number of times."""

    def generate(self, profile: ReferenceProfile, prefix_name: str) -> PaymentReference:
        prefix = profile.prefix_for(prefix_name)
        if profile.suffix_length is None or profile.alphabet is None:
            raise ValueError(f"reference profile v{profile.version} cannot generate codes")
        alphabet = profile.alphabet
        suffix = "".join(secrets.choice(alphabet) for _ in range(profile.suffix_length))
        return PaymentReference(prefix + suffix)
