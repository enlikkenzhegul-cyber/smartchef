from django.urls import path
from . import views

app_name = "recipes"
urlpatterns = [
    path("", views.index, name="index"),
    path("save/", views.save_recipe, name="save_recipe"),
    path("reject/", views.reject_recipe, name="reject_recipe"),
    path("accept-candidate/", views.accept_candidate, name="accept_candidate"),
    path("reject-candidate/", views.reject_candidate, name="reject_candidate"),
    path("suggest/accept/", views.accept_pending, name="accept_pending"),
    path("suggest/reject/", views.reject_pending, name="reject_pending"),
    path("undo/", views.undo_last, name="undo_last"),
    path("saved/", views.saved_recipes, name="saved_recipes"),
]