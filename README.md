## What this fork changes

I wanted to include the amazing [Bee's character dictionary](https://characterdictionary.tokyo/) in the build-dict script while keeping its optimizations like sorting JMDict entries by frequency. If you want to use this, simply navigate to `/src/meikipop/scripts/build_dictionary.py` and in URLS constant change the `vndb_user=u123456` query string to your own VNDB ID. You can also use AniList Username instead, like this: `anilist_user=Username`. Bee character dictionary website has various settings, so you can also modify those and change this URL altogether. If that's the case, in the downloaded zip look into `index.json` file for "downloadUrl" field. The default URL I provided uses very minimalist settings.

Then just run `meikipop build-dict` command and your character dictionary should be included. I disabled caching for downloads of dicts, so that every time `build-dict` is called, all dicts will be redownloaded. This is desirable especially for the charater dictionary, but I do want to keep my other dicts up to date as well, which the former caching prevented.

The author (rtr46) has a nice release setup in place, so I recommend just installing meikipop normally and then copying the `/src/meikipop/scripts/build_dictionary.py` over.

For the original readme, refer to [rtr46's repository](https://github.com/rtr46/meikipop).
