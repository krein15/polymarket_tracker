"""Какие рынки трекер вообще рассматривает.

Почему отдельный модуль
-----------------------
Правило жило методом внутри детектора, и его спрашивала только ветка
score. Быстрая полоса и подтверждение по погоне решали сами, то есть не
фильтровали ничего. На живых данных это выглядело так: из 466 сигналов по
киберспорту 357 пришли через score и ещё 109 — мимо фильтра, через две
другие ветки.

Замер, из-за которого правило стало важным (425 сделок с замером цены
входа и исходом):

    киберспорт внутри матча   n=308   ROI -15.2%   перевес -6.2 пп
    всё остальное             n=117   ROI  -6.3%   перевес +0.1 пп

И главное: в киберспорте перевес отрицательный у САМОГО трейдера — по его
собственной цене -3.8 пп. Копировать там нечего. В живом матче нет частной
информации: всё уже на экране, а кто быстрее смотрит трансляцию, тот и
"инсайдер".
"""
from __future__ import annotations


def market_tags(market) -> set:
    """Категория и теги одним множеством.

    Смотреть надо на оба: рынок LoL приходит с тегами
    {esports, league-of-legends, games, sports}, и по одной лишь категории
    "esports" фильтр "sports" его не поймает.
    """
    if market is None:
        return set()
    category = getattr(market, "category", "") or ""
    tags = getattr(market, "tags", None) or ()
    return {category} | set(tags)


def is_ignored(market, allowed_tags: set, ignored_categories: set) -> bool:
    """Рынок в чёрном списке, с учётом явных разрешений.

    ALLOWED_TAGS перевешивает: киберспорт помечен и как sports, и вернуть
    его иначе нельзя, не открыв заодно весь обычный спорт.

    Рынок неизвестен (Gamma не ответила) — НЕ игнорируем: молчать из-за
    недоступности справочника хуже, чем прислать лишний сигнал.
    """
    tags = market_tags(market)
    if not tags:
        return False
    if allowed_tags & tags:
        return False
    return bool(ignored_categories & tags)


def is_ignored_for(market, config) -> bool:
    """То же, но с настройками прямо из конфига."""
    return is_ignored(
        market,
        getattr(config, "allowed_tags", set()) or set(),
        getattr(config, "ignored_categories", set()) or set(),
    )
