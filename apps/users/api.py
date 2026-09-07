from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils.text import slugify
from rest_framework import generics, serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .services import GoogleTokenError, verify_google_id_token

User = get_user_model()


class TokenObtainPairThrottledView(TokenObtainPairView):
    """Login con throttling propio (scope `auth`) para frenar fuerza bruta."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


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
