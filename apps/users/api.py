from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import generics, serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenObtainSerializer
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.tokens import RefreshToken, Token
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import TwoFactorAuth, _generate_secret
from .services import GoogleTokenError, verify_google_id_token

User = get_user_model()


class MFAChallengeToken(Token):
    """Token de un solo uso y vida corta que certifica "la contraseña ya se
    validó, falta el código de 2FA" -- NO es un access token real: no sirve
    contra el resto del API (otro `token_type`, así que
    `JWTAuthentication` lo rechaza de plano) y sólo lo acepta
    `TwoFactorVerifyView`. Vive 5 minutos: de sobra para tipear un código,
    poco margen si se filtra."""

    token_type = "mfa_challenge"
    lifetime = timedelta(minutes=5)


def _mfa_challenge_token(user) -> str:
    token = MFAChallengeToken.for_user(user)
    return str(token)


def _user_from_mfa_token(raw_token: str):
    """Decodifica y valida el challenge; devuelve el User o lanza
    `serializers.ValidationError` con un mensaje ya listo para el campo."""
    try:
        token = MFAChallengeToken(raw_token)
    except TokenError:
        raise serializers.ValidationError({"mfa_token": "Inválido o vencido -- iniciá sesión de nuevo."})
    user_id = token.get(jwt_settings.USER_ID_CLAIM)
    user = User.objects.filter(**{jwt_settings.USER_ID_FIELD: user_id}, is_active=True).first()
    if user is None:
        raise serializers.ValidationError({"mfa_token": "Inválido."})
    return user


class TwoFactorAwareTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Igual que el login de siempre, salvo que si el usuario tiene 2FA
    activo NO emite tokens reales todavía -- deja `self.mfa_required=True`
    y un `self.mfa_token` (challenge) para el segundo paso
    (`TwoFactorVerifyView`). La vista es quien arma la respuesta distinta
    en cada caso (ver `TokenObtainPairThrottledView.post`)."""

    def validate(self, attrs):
        # La autenticación de usuario/contraseña de siempre: fija
        # `self.user` o lanza AuthenticationFailed -- sin esto ninguna
        # contraseña mala cambia de comportamiento por tener 2FA o no.
        TokenObtainSerializer.validate(self, attrs)

        two_factor = TwoFactorAuth.objects.filter(user=self.user, enabled=True).first()
        if two_factor is not None:
            self.mfa_required = True
            self.mfa_token = _mfa_challenge_token(self.user)
            return {}

        self.mfa_required = False
        refresh = self.get_token(self.user)
        return {"refresh": str(refresh), "access": str(refresh.access_token)}


class TokenObtainPairThrottledView(TokenObtainPairView):
    """Login con throttling propio (scope `auth`) para frenar fuerza bruta.
    Con 2FA activo, no devuelve tokens en este paso -- ver
    `TwoFactorAwareTokenObtainPairSerializer` y `TwoFactorVerifyView`."""

    serializer_class = TwoFactorAwareTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.mfa_required:
            return Response(
                {"two_factor_required": True, "mfa_token": serializer.mfa_token}, status=200
            )
        return Response(serializer.validated_data, status=200)


class TwoFactorVerifySerializer(serializers.Serializer):
    mfa_token = serializers.CharField()
    code = serializers.CharField()


class TwoFactorVerifyView(generics.GenericAPIView):
    """Segundo paso del login cuando `TokenObtainPairThrottledView` devolvió
    `two_factor_required`: el `mfa_token` de ese paso + un código (TOTP o
    de respaldo) a cambio de los tokens reales."""

    serializer_class = TwoFactorVerifySerializer
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = _user_from_mfa_token(serializer.validated_data["mfa_token"])

        two_factor = TwoFactorAuth.objects.filter(user=user, enabled=True).first()
        if two_factor is None:
            # Se desactivó el 2FA entre el primer paso y este -- el
            # challenge ya no aplica, hay que loguearse de nuevo.
            raise serializers.ValidationError({"mfa_token": "Inválido -- iniciá sesión de nuevo."})

        if not two_factor.verify_code(serializer.validated_data["code"]):
            raise serializers.ValidationError({"code": "Código inválido."})

        return Response({"user": UserSerializer(user).data, **_tokens_for(user)}, status=200)


class TwoFactorStatusSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()


class TwoFactorSetupSerializer(serializers.Serializer):
    secret = serializers.CharField(read_only=True)
    otpauth_url = serializers.CharField(read_only=True)


class TwoFactorEnableSerializer(serializers.Serializer):
    code = serializers.CharField()


class TwoFactorPasswordSerializer(serializers.Serializer):
    password = serializers.CharField()

    def validate_password(self, value):
        if not check_password(value, self.context["request"].user.password):
            raise serializers.ValidationError("Contraseña incorrecta.")
        return value


class TwoFactorStatusView(generics.GenericAPIView):
    """GET: si el usuario ya tiene 2FA activo (no expone el secreto ni los
    códigos de respaldo -- eso sólo se ve una vez, al activarlo)."""

    serializer_class = TwoFactorStatusSerializer
    permission_classes = [IsAuthenticated]

    def get(self, request):
        enabled = TwoFactorAuth.objects.filter(user=request.user, enabled=True).exists()
        return Response({"enabled": enabled})


class TwoFactorSetupView(generics.GenericAPIView):
    """Arranca (o reinicia) la activación: genera un secreto NUEVO, todavía
    sin confirmar (`enabled=False`, no sirve para el login hasta
    `TwoFactorEnableView`). Devuelve el secreto y la URI `otpauth://` para
    que el cliente arme el QR."""

    serializer_class = TwoFactorSetupSerializer
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        existing = TwoFactorAuth.objects.filter(user=request.user).first()
        if existing is not None and existing.enabled:
            raise serializers.ValidationError(
                "Ya tenés 2FA activo -- desactivalo primero para volver a configurarlo."
            )
        two_factor, _ = TwoFactorAuth.objects.update_or_create(
            user=request.user, defaults={"secret": _generate_secret()}
        )
        return Response(
            {"secret": two_factor.secret, "otpauth_url": two_factor.provisioning_uri()}
        )


class TwoFactorEnableView(generics.GenericAPIView):
    """Confirma la activación con un código real generado a partir del
    secreto de `TwoFactorSetupView`. Devuelve los códigos de respaldo EN
    CLARO -- única vez que se ven; el cliente tiene que mostrarlos para que
    el usuario los guarde."""

    serializer_class = TwoFactorEnableSerializer
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        two_factor = TwoFactorAuth.objects.filter(user=request.user).first()
        if two_factor is None:
            raise serializers.ValidationError("Primero generá un secreto (POST /auth/2fa/setup/).")
        if two_factor.enabled:
            raise serializers.ValidationError("Ya tenés 2FA activo.")
        if not two_factor.verify_totp(serializer.validated_data["code"]):
            raise serializers.ValidationError({"code": "Código inválido."})

        backup_codes = two_factor.generate_backup_codes()
        two_factor.enabled = True
        two_factor.confirmed_at = timezone.now()
        two_factor.save(update_fields=["enabled", "backup_codes", "confirmed_at", "updated_at"])

        return Response({"enabled": True, "backup_codes": backup_codes})


class TwoFactorDisableView(generics.GenericAPIView):
    """Requiere la contraseña (no un código de 2FA): si el usuario perdió
    el teléfono con la app de autenticación, sigue pudiendo desactivarlo
    sabiendo su contraseña -- lo contrario lo dejaría afuera de su propia
    cuenta."""

    serializer_class = TwoFactorPasswordSerializer
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        two_factor = TwoFactorAuth.objects.filter(user=request.user, enabled=True).first()
        if two_factor is None:
            raise serializers.ValidationError("No tenés 2FA activo.")
        two_factor.enabled = False
        two_factor.backup_codes = []
        two_factor.confirmed_at = None
        two_factor.save(update_fields=["enabled", "backup_codes", "confirmed_at", "updated_at"])
        return Response(status=204)


class TwoFactorRegenerateBackupCodesView(generics.GenericAPIView):
    """Invalida los códigos de respaldo viejos y genera un set nuevo (p. ej.
    porque se usaron casi todos, o se sospecha que alguien más los vio)."""

    serializer_class = TwoFactorPasswordSerializer
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        two_factor = TwoFactorAuth.objects.filter(user=request.user, enabled=True).first()
        if two_factor is None:
            raise serializers.ValidationError("No tenés 2FA activo.")
        backup_codes = two_factor.generate_backup_codes()
        two_factor.save(update_fields=["backup_codes", "updated_at"])
        return Response({"backup_codes": backup_codes})


class TokenRefreshThrottledView(TokenRefreshView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ("id", "username", "email", "password", "first_name", "last_name")
        read_only_fields = ("id",)

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Ya existe una cuenta con ese correo.")
        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class UserSerializer(serializers.ModelSerializer):
    """Datos del usuario autenticado (`/auth/me/`). El username no se cambia aquí."""

    class Meta:
        model = User
        fields = (
            "id", "username", "email", "first_name", "last_name",
            "profile_photo_url", "google_linked", "date_joined",
        )
        read_only_fields = (
            "id", "username", "profile_photo_url", "google_linked", "date_joined",
        )

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("Ya existe una cuenta con ese correo.")
        return value


def _tokens_for(user):
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


class RegisterView(generics.CreateAPIView):
    """Alta de cuenta. Devuelve el usuario y un par de tokens JWT listos para usar."""

    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {"user": UserSerializer(user).data, **_tokens_for(user)},
            status=201,
        )


class MeView(generics.RetrieveUpdateAPIView):
    """GET / PATCH del usuario autenticado."""

    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


def _unique_username_from_email(email):
    base = slugify(email.split("@", 1)[0]) or "usuario"
    username = base
    n = 1
    while User.objects.filter(username=username).exists():
        n += 1
        username = f"{base}{n}"
    return username


class GoogleIdTokenSerializer(serializers.Serializer):
    id_token = serializers.CharField()


class GoogleLoginView(generics.GenericAPIView):
    """
    "Continuar con Google": verifica el id_token del cliente contra Google y
    devuelve un par de tokens JWT propios, igual que register/login.

    Solo sirve para CREAR cuentas nuevas (o volver a entrar a una ya creada
    por Google): primera vez con ese correo -> crea la cuenta (username
    derivado del correo, sin password utilizable -- solo entra por Google) y
    le copia el nombre y la foto de perfil del token. Si el correo ya es de
    una cuenta usuario/contraseña que nunca vinculó Google (`google_linked`),
    NO se entra sola -- hay que iniciar sesión con la contraseña y vincular a
    propósito desde Herramientas → Cuenta (ver `GoogleLinkView`); si no, un
    correo de Google ajeno bastaría para entrar a cualquier cuenta con ese
    correo.
    """

    serializer_class = GoogleIdTokenSerializer
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            claims = verify_google_id_token(serializer.validated_data["id_token"])
        except GoogleTokenError as exc:
            raise serializers.ValidationError({"id_token": str(exc)})

        email = claims["email"]
        picture = claims.get("picture") or ""
        user = User.objects.filter(email__iexact=email).first()
        created = user is None

        if created:
            user = User.objects.create_user(
                username=_unique_username_from_email(email),
                email=email,
                first_name=claims.get("given_name") or "",
                last_name=claims.get("family_name") or "",
                profile_photo_url=picture,
                google_linked=True,
            )
            user.set_unusable_password()
            user.save(update_fields=["password"])
        elif not user.google_linked:
            raise serializers.ValidationError(
                {
                    "id_token": (
                        "Ya existe una cuenta con ese correo. Inicia sesión con tu "
                        "contraseña y vincula Google desde Herramientas → Cuenta."
                    )
                }
            )
        elif picture and not user.profile_photo_url:
            user.profile_photo_url = picture
            user.save(update_fields=["profile_photo_url"])

        return Response(
            {"user": UserSerializer(user).data, "created": created, **_tokens_for(user)},
            status=200,
        )


class GoogleLinkView(generics.GenericAPIView):
    """
    Herramientas → Cuenta → "Vincular cuenta de Google": el usuario YA está
    autenticado (con su contraseña) y trae un id_token de Google recién
    obtenido. Si el correo de ese token coincide con el de su cuenta, la
    marca `google_linked` -- de ahí en más también puede entrar por
    "Continuar con Google" con ese correo (ver `GoogleLoginView`).
    """

    serializer_class = GoogleIdTokenSerializer
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            claims = verify_google_id_token(serializer.validated_data["id_token"])
        except GoogleTokenError as exc:
            raise serializers.ValidationError({"id_token": str(exc)})

        email = claims["email"]
        user = request.user
        if email.lower() != user.email.lower():
            raise serializers.ValidationError(
                {"id_token": "Esa cuenta de Google no tiene el mismo correo que tu cuenta."}
            )

        update_fields = ["google_linked"]
        user.google_linked = True
        picture = claims.get("picture") or ""
        if picture and not user.profile_photo_url:
            user.profile_photo_url = picture
            update_fields.append("profile_photo_url")
        user.save(update_fields=update_fields)

        return Response(UserSerializer(user).data, status=200)
