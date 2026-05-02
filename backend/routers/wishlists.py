from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models.user import User
from backend.models.wishlist import WishList, WishListItem
from backend.schemas.wishlist import (
    WishListCreate,
    WishListItemCreate,
    WishListItemOut,
    WishListItemUpdate,
    WishListOut,
    WishListUpdate,
)

router = APIRouter(prefix="/api/wishlists", tags=["wishlists"])


def _get_wishlist_for_user(wishlist_id: int, user_id: int, db: Session) -> WishList:
    wl = db.query(WishList).filter(
        WishList.id == wishlist_id, WishList.user_id == user_id
    ).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Wish list not found")
    return wl


# --- Wish Lists ---

@router.get("/", response_model=List[WishListOut])
def list_wishlists(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return db.query(WishList).filter(WishList.user_id == current_user.id).all()


@router.post("/", response_model=WishListOut, status_code=status.HTTP_201_CREATED)
def create_wishlist(
    wl_in: WishListCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wl = WishList(**wl_in.model_dump(), user_id=current_user.id)
    db.add(wl)
    db.commit()
    db.refresh(wl)
    return wl


@router.get("/{wishlist_id}", response_model=WishListOut)
def get_wishlist(
    wishlist_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_wishlist_for_user(wishlist_id, current_user.id, db)


@router.put("/{wishlist_id}", response_model=WishListOut)
def update_wishlist(
    wishlist_id: int,
    wl_update: WishListUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wl = _get_wishlist_for_user(wishlist_id, current_user.id, db)
    for field, value in wl_update.model_dump(exclude_unset=True).items():
        setattr(wl, field, value)
    db.commit()
    db.refresh(wl)
    return wl


@router.delete("/{wishlist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_wishlist(
    wishlist_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wl = _get_wishlist_for_user(wishlist_id, current_user.id, db)
    db.delete(wl)
    db.commit()


# --- Wish List Items ---

@router.get("/{wishlist_id}/items", response_model=List[WishListItemOut])
def list_items(
    wishlist_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_wishlist_for_user(wishlist_id, current_user.id, db)
    return db.query(WishListItem).filter(WishListItem.wishlist_id == wishlist_id).all()


@router.post(
    "/{wishlist_id}/items",
    response_model=WishListItemOut,
    status_code=status.HTTP_201_CREATED,
)
def create_item(
    wishlist_id: int,
    item_in: WishListItemCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_wishlist_for_user(wishlist_id, current_user.id, db)
    item = WishListItem(**item_in.model_dump(), wishlist_id=wishlist_id)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/{wishlist_id}/items/{item_id}", response_model=WishListItemOut)
def update_item(
    wishlist_id: int,
    item_id: int,
    item_update: WishListItemUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_wishlist_for_user(wishlist_id, current_user.id, db)
    item = db.query(WishListItem).filter(
        WishListItem.id == item_id, WishListItem.wishlist_id == wishlist_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Wish list item not found")
    for field, value in item_update.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{wishlist_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(
    wishlist_id: int,
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_wishlist_for_user(wishlist_id, current_user.id, db)
    item = db.query(WishListItem).filter(
        WishListItem.id == item_id, WishListItem.wishlist_id == wishlist_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Wish list item not found")
    db.delete(item)
    db.commit()
