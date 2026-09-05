# TrackTrack
Frontend + backend to gather, predict, and display Long Island Rail Road track numbers

If you've ever ridden the LIRR out of Penn Station you might know that they only post track numbers as little as a couple minutes before the train is scheduled to depart. I could yap all day about NYC's commuter rail infrastructure and why that it is the way that it is, but I digress. Despite the timeframe that the tracks are posted, they somewhat follow a pattern. This project has the goal of trying to understand these patterns.

There's three main parts of this project. Gathering the data, interpreting the data, and displaying the data.

## Gathering the data

There's a lot of ways MTA publishes their data and to be honest it was confusing and overwhelming. I wish I understood GTFS but my brain is too small. Instead I reverse engineered the API that is used on radar.mta.info which is an API I understand. The only parameter is the station. It returns the upcoming trains for that station as well as the track if its been posted, as well as some other stuff. Gathering the data is a simple task of pinging that endpoint every 25 seconds and storing it in a SQLite database. There's two tables within this database. The trips table shows all of the trains that are scheduled by the LIRR per station. Each row includes things like train number, scheduled departure, day of the week, as well as destination. The events table is a log of only when the track first appears, and when it is posted, which helps us understand when the track might be posted in the future and of course the track number. 

This is lirr_collector.py

## Interpreting the data

Once we have the data, we need a way to predict the track in the future. The best way to do this would probably be to have large amounts of data and then to build some sort of machine learning model to predict the track number. The problem is I don't have the knowledge (but will 🔜™️) to do such a machine learning model and I also don't have the sheer data, yet. I would love to do ML in the future with this data! HMU!! Anyways, the way it currently works is every night it builds a baseline table for what the general predictions should be for the next day's tracks. The baseline isn't smart enough though. Since we know multiple trains can't have the same track number at the same time, when the API is called, the program will recalculate the next likely track. A ML model would be better at understanding these patterns but this works okay. 

This is predictor.py

## Displaying the data

Since everything is already in Python, I opted to utilize FastAPI. FastAPI isn't amazing. It takes a lot more resources than say a node.js API but since all of the code is Python it makes the most sense to continue Python. I also really love how easy FastAPI is to use. The primary two endpoints are the predict and upcoming endpoints. The upcoming endpoint is similar to the MTA radar endpoint but instead includes predicted track numbers. It also includes some other parameters like a limit for how many upcoming trains and the window for how far off you want the predictions. Additionally, the predict endpoint exists so you can query a specifc train number and station to identify the prediction will be even if it isn't on the upcoming board yet. The frontend app (index.html) simply queries the API and then displays the data in a format similar to what LIRR already offers on the radar site.

## Using the app

The app is designed to be similar to the radar.mta.info/departures board. The colors coincide with what LIRR also uses for each line. 
