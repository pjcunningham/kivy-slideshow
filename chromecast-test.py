import pychromecast

if __name__ == '__main__':

    casts = pychromecast.get_chromecasts()
    media_controller = casts[0].media_controller if len(casts) > 0 else None
    print media_controller